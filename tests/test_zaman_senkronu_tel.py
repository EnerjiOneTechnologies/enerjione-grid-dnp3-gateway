"""ZAMAN SENKRONU — TEL SEVIYESINDE KANIT (1.16.0).

KOK NEDEN (sahada gorulen, 1.15.1)
----------------------------------
`IMasterApplication.Now()` `opendnp3.DNPTime` donduruyordu. Binding
`UTCTimestamp` bekler; cast edemeyince SESSIZCE `UTCTimestamp(0)` kullanir ve
kutuphane bu degeri kontrol etmeden yazar. Cihaza her NEED_TIME'da
1970-01-01 00:00:00.000 UTC gitti: saat 00:00'a donuyor, Dial-In yalnizca
ilk slotta calisiyordu. Mevcut testler bunu YAKALAMADI: yalnizca FC kodunu
ve nesne tipini kontrol ediyor, yazilan DEGERE bakmiyordu.

BU DOSYANIN KURALLARI
---------------------
* Master, gateway'in GERCEK kod yoluyla kurulur: `build_adapter(Settings)`
  -> `read_device` -> `_ensure_master` -> `_ManagedMaster` ->
  `_make_master_app`. `Now()`u override eden bir test sinifi YOKTUR.
* Karsida gercek bir yadnp3 outstation vardir: NEED_TIME ister,
  `SupportsWriteAbsoluteTime=True`, yazilan degeri kaydeder.
* Yazilan deger HAM CERCEVEDEN cozulur (G50V1'in 6 baytlik alani) — API'nin
  soylediğine degil tele cikana bakilir.

Topoloji Horstmann sahasiyla ayni: cihaz (TCP client) -> proxy -> gateway
(TCP server, `initiating`).
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import pytest

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 wheel kurulu degil")

from dnp3_gateway.adapters import dnp3_yadnp3_master as ym  # noqa: E402
from dnp3_gateway.adapters.factory import build_adapter  # noqa: E402
from dnp3_gateway.backend import DeviceConfig  # noqa: E402
from dnp3_gateway.config import Settings  # noqa: E402
from dnp3_gateway.resource_guard import CLOCK_UNSAFE_SKEW_SEC, ClockGuard  # noqa: E402

from .conftest import make_signal  # noqa: E402

SINYALLER = [make_signal("master.actual_current", object_group=30, index=0)]
KOD = "SN2-ZAMAN"
FC_WRITE = 0x02
FC_DELAY_MEASURE = 0x17
FC_RECORD_CURRENT_TIME = 0x18


def _bos_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _bekle(kosul, timeout: float, aralik: float = 0.2):
    son = time.monotonic() + timeout
    while time.monotonic() < son:
        if sonuc := kosul():
            return sonuc
        time.sleep(aralik)
    return None


def _date(offset_sec: float = 0.0) -> str:
    return format_datetime(datetime.now(timezone.utc) + timedelta(seconds=offset_sec))


# ==========================================================================
# Tel: proxy + DNP3 cerceve cozucu
# ==========================================================================


class _YakalayanProxy:
    """cihaz -> proxy -> gateway; gateway -> cihaz yonundeki HER bayti saklar.

    Birden fazla baglantiyi kabul eder: master yeniden kurulunca cihaz yeniden
    baglanir ve yakalama kesintisiz surer.
    """

    def __init__(self, gateway_port: int) -> None:
        self.gateway_port = gateway_port
        self.port = _bos_port()
        self._lock = threading.Lock()
        self._gw_giden = bytearray()
        self._kapandi = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.port))
        self._srv.listen(8)
        threading.Thread(target=self._kabul, daemon=True).start()

    def _kabul(self) -> None:
        while not self._kapandi.is_set():
            try:
                cihaz, _ = self._srv.accept()
            except OSError:
                return
            try:
                gw = socket.create_connection(("127.0.0.1", self.gateway_port), timeout=5)
            except OSError:
                cihaz.close()
                continue
            threading.Thread(target=self._pompa, args=(gw, cihaz, True), daemon=True).start()
            threading.Thread(target=self._pompa, args=(cihaz, gw, False), daemon=True).start()

    def _pompa(self, kaynak: socket.socket, hedef: socket.socket, gw_yonu: bool) -> None:
        try:
            while True:
                veri = kaynak.recv(4096)
                if not veri:
                    break
                if gw_yonu:
                    with self._lock:
                        self._gw_giden += veri
                hedef.sendall(veri)
        except OSError:
            pass
        finally:
            # shutdown() SART: Linux'ta baska bir thread'in recv'de bekledigi
            # soketi close() etmek FIN GONDERMEZ ve o recv'i uyandirmaz; cihaz
            # kopusu hic gormez (Windows'ta close yeterliydi, CI'da yakalandi).
            for s in (kaynak, hedef):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

    def gw_istekleri(self) -> list[bytes]:
        """Gateway'in gonderdigi uygulama katmani istekleri: `[AC, FC, nesneler...]`."""
        with self._lock:
            akis = bytes(self._gw_giden)
        return [u[1:] for u in _kullanici_verileri(akis) if len(u) >= 3]

    def kapat(self) -> None:
        self._kapandi.set()
        try:
            self._srv.close()
        except OSError:
            pass


def _kullanici_verileri(akis: bytes) -> list[bytes]:
    """Ham TCP akisindan her link cercevesinin CRC'siz kullanici verisi.

    Link cercevesi: 05 64 LEN CTRL DST(2) SRC(2) CRC(2), ardindan 16 baytlik
    bloklar + her blogun 2 baytlik CRC'si. Kullanici verisinin ilk bayti
    transport basligidir (tek fragment: FIR|FIN).
    """
    sonuc: list[bytes] = []
    i = 0
    while i + 10 <= len(akis):
        if akis[i] != 0x05 or akis[i + 1] != 0x64:
            i += 1
            continue
        n_kullanici = akis[i + 2] - 5
        blok = (n_kullanici + 15) // 16
        toplam = 10 + n_kullanici + 2 * blok
        if i + toplam > len(akis):
            break
        govde = akis[i + 10 : i + toplam]
        veri = b"".join(govde[b * 18 : b * 18 + 18][:-2] for b in range(blok))
        if veri:
            sonuc.append(veri)
        i += toplam
    return sonuc


def _fc(istek: bytes) -> int:
    return istek[1]


def _g50v1_degerleri(istekler: list[bytes]) -> list[int]:
    """FC=2 WRITE g50v1 qualifier 0x07 count=1 -> 48 bit ms (little-endian)."""
    degerler = []
    for r in istekler:
        if _fc(r) == FC_WRITE and r[2:5] == b"\x32\x01\x07" and r[5] == 1:
            degerler.append(int.from_bytes(r[6:12], "little"))
    return degerler


def _g50_varyasyonlari(istekler: list[bytes]) -> set[int]:
    return {r[3] for r in istekler if _fc(r) == FC_WRITE and r[2] == 50}


# ==========================================================================
# Cihaz: NEED_TIME isteyen gercek outstation
# ==========================================================================


class _ZamanIsteyenUygulama(opendnp3.IOutstationApplication):
    def __init__(self, *, saat_ister: bool = True) -> None:
        super().__init__()
        self.need_time = saat_ister
        self.yazilanlar: list[int] = []
        self.alis_anlari: list[float] = []

    def GetApplicationIIN(self):  # noqa: N802 — kutuphane imzasi
        iin = opendnp3.ApplicationIIN()
        iin.needTime = self.need_time
        return iin

    def SupportsWriteAbsoluteTime(self):  # noqa: N802 — kutuphane imzasi
        return True

    def WriteAbsoluteTime(self, ts):  # noqa: N802 — kutuphane imzasi
        self.alis_anlari.append(time.time() * 1000)
        self.yazilanlar.append(int(getattr(ts, "msSinceEpoch", ts)))
        self.need_time = False
        return True


class _KomutIsleyici(opendnp3.ICommandHandler):
    def __init__(self) -> None:
        super().__init__()

    def Begin(self) -> None:  # noqa: N802
        pass

    def End(self) -> None:  # noqa: N802
        pass


class _Cihaz:
    """Horstmann taklidi (initiating): cihazin KENDISI master'a baglanir."""

    def __init__(self, hedef_port: int, *, unsolicited: bool, saat_ister: bool = True) -> None:
        self.uygulama = _ZamanIsteyenUygulama(saat_ister=saat_ister)
        self._manager = opendnp3.DNP3Manager(2)
        kanal = self._manager.AddOutstationTCPClient(
            "os_ch",
            opendnp3.levels.NORMAL,
            opendnp3.ChannelRetry(opendnp3.TimeDuration.Milliseconds(200), opendnp3.TimeDuration.Seconds(1)),
            [opendnp3.IPEndpoint("127.0.0.1", hedef_port)],
            "0.0.0.0",
            None,
        )
        cfg = opendnp3.OutstationStackConfig(opendnp3.DatabaseConfig(4))
        cfg.link.LocalAddr = 10
        cfg.link.RemoteAddr = 1
        # Smart'ta master tarama gondermez; Horstmann raporunu kendisi
        # (unsolicited) gonderir ve IIN1.4 o yanitla gelir.
        cfg.outstation.params.allowUnsolicited = unsolicited
        self._komut = _KomutIsleyici()
        self._os = kanal.AddOutstation("os", self._komut, self.uygulama, cfg)
        self._os.Enable()

    def kapat(self) -> None:
        try:
            self._manager.Shutdown()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# Kurulum: gateway'in GERCEK okuyucusu (fabrika + varsayilan ayarlar)
# ==========================================================================


def _korumayi_bagla(okuyucu: Any, guard: Any):
    """Saat korumasini okuyucunun GERCEKTEN kullandigi adapter modulune bagla.

    Fabrika adapter'i cagri aninda import eder; `sys.modules` baska bir testte
    temizlendiyse o modul bu dosyanin `ym`sinden FARKLI bir nesne olabilir.
    Donen: eski degeri geri koyan fonksiyon.
    """
    modul = sys.modules[type(okuyucu).__module__]
    onceki = modul._clock_guard_ref.get("guard")
    modul.set_clock_guard(guard)
    return lambda: modul.set_clock_guard(onceki)


@pytest.fixture
def saat_korumasi():
    """Adapter'in modul-duzeyi saat korumasini test boyunca ayarlar."""
    onceki = ym._clock_guard_ref.get("guard")
    g = ClockGuard()
    ym.set_clock_guard(g)
    try:
        yield g
    finally:
        ym.set_clock_guard(onceki)


@pytest.fixture
def saha(tmp_path, monkeypatch):
    """`(kur, kapat)` — her test kendi politikasi/ayariyla saha kurar."""
    kaynaklar: list[Any] = []
    geri_al: list[Any] = []

    def kur(
        *,
        policy: str = "continuous",
        time_sync: str | None = None,
        profil: str = "horstmann_sn_2_0",
        saat_ister: bool = True,
        her_oturum: bool | None = None,
    ):
        monkeypatch.setenv("GATEWAY_MODE", "dnp3")
        monkeypatch.setenv("DNP3_LIBRARY", "yadnp3")
        monkeypatch.setenv("GATEWAY_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("DNP3_EVENT_SCAN_INTERVAL_SEC", "1")
        monkeypatch.setenv("DNP3_EVENT_BASELINE_INTERVAL_SEC", "5")
        if time_sync is None:
            monkeypatch.delenv("DNP3_TIME_SYNC", raising=False)
        else:
            monkeypatch.setenv("DNP3_TIME_SYNC", time_sync)
        if her_oturum is None:
            monkeypatch.delenv("DNP3_TIME_WRITE_EACH_SESSION", raising=False)
        else:
            monkeypatch.setenv("DNP3_TIME_WRITE_EACH_SESSION", "true" if her_oturum else "false")
        ayarlar = Settings(_env_file=None)  # type: ignore[call-arg]
        okuyucu = build_adapter(ayarlar)
        kaynaklar.append(okuyucu)
        geri_al.append(_korumayi_bagla(okuyucu, ym._clock_guard_ref.get("guard")))
        gw_port = _bos_port()
        cihaz_cfg = DeviceConfig(
            code=KOD,
            name="sn2",
            ip_address="127.0.0.1",
            dnp3_address=10,
            master_address=1,
            ip_endpoint_type="initiating",
            master_ip_port=gw_port,
            session_policy=policy,
            signal_profile=profil,
        )
        proxy = _YakalayanProxy(gw_port)
        kaynaklar.append(proxy)
        okuyucu.read_device(device=cihaz_cfg, signals=SINYALLER)  # master'i kur
        cihaz = _Cihaz(proxy.port, unsolicited=(policy == "smart"), saat_ister=saat_ister)
        kaynaklar.append(cihaz)
        return okuyucu, cihaz_cfg, cihaz, proxy, ayarlar

    yield kur
    for k in reversed(kaynaklar):
        try:
            k.close() if hasattr(k, "close") else k.kapat()
        except Exception:  # noqa: BLE001
            pass
    for geri in geri_al:
        geri()


def _bagli(okuyucu, cihaz_cfg) -> bool:
    mm = okuyucu._masters.get(cihaz_cfg.code)
    return mm is not None and mm.cache.is_connected()


def _cihaz_saat_istedi(okuyucu, cihaz_cfg) -> bool:
    mm = okuyucu._masters.get(cihaz_cfg.code)
    return mm is not None and mm.cache.iin_snapshot().get("need_time") is True


# ==========================================================================
# KABUL 1 — NonLAN + koruma `safe`: tam bir FC=23 + bir G50V1, deger dogru
# ==========================================================================


@pytest.mark.parametrize("policy", ["continuous", "smart"])
def test_varsayilan_nonlan_dogru_utc_yazar_ve_need_time_temizlenir(saha, saat_korumasi, caplog, policy):
    saat_korumasi.observe_http_date(_date())
    assert saat_korumasi.clock_state == "safe"
    with caplog.at_level(logging.INFO, logger=ym.logger.name):
        okuyucu, cihaz_cfg, cihaz, proxy, ayarlar = saha(policy=policy)
        # VARSAYILAN AYARLAR: DNP3_TIME_SYNC verilmedi -> nonlan.
        assert ayarlar.dnp3_time_sync == "nonlan"
        assert okuyucu._masters[KOD].time_sync == "nonlan"

        assert _bekle(lambda: cihaz.uygulama.yazilanlar, 30.0), "NEED_TIME'a ragmen cihaza saat YAZILMADI"
        time.sleep(2.0)  # yazimdan sonra tekrar olmadigini gormek icin dinle

    istekler = proxy.gw_istekleri()
    fclar = [_fc(r) for r in istekler]
    assert fclar.count(FC_DELAY_MEASURE) == 1, f"tam bir FC=23 bekleniyordu; FC'ler: {fclar}"
    g50v1 = _g50v1_degerleri(istekler)
    assert len(g50v1) == 1, f"tam bir G50V1 yazmasi bekleniyordu: {g50v1}"
    assert FC_RECORD_CURRENT_TIME not in fclar, "nonlan yolunda FC=24 gonderildi"
    assert _g50_varyasyonlari(istekler) == {1}, "nonlan yolunda G50V1 disinda zaman nesnesi yazildi"

    # DEGER — HAM CERCEVEDEN: 1.15.1'de bu alan `00 00 00 00 00 00` idi.
    tel_ms = g50v1[0]
    assert tel_ms == cihaz.uygulama.yazilanlar[0], "teldeki deger ile cihazin aldigi farkli"
    assert abs(tel_ms - cihaz.uygulama.alis_anlari[0]) <= 2000, (
        f"yazilan {ym._iso_utc(tel_ms)} gercek UTC degil (fark {tel_ms - cihaz.uygulama.alis_anlari[0]:.0f} ms)"
    )
    assert cihaz.uygulama.need_time is False, "senkronizasyondan sonra NEED_TIME temizlenmedi"

    mesajlar = [r.getMessage() for r in caplog.records]
    yazim = [m for m in mesajlar if "dnp3_time_sync_write" in m]
    assert len(yazim) == 1 and f"device={KOD}" in yazim[0] and "method=nonlan" in yazim[0], yazim
    assert any("dnp3_time_sync_ok" in m for m in mesajlar)
    assert not any("dnp3_time_sync_invalid_value" in m for m in mesajlar)


# ==========================================================================
# KABUL 2 — koruma `unknown` / `unsafe`: 10 sn icinde HIC G50V1 yok
# ==========================================================================


@pytest.mark.parametrize("durum", ["unknown", "unsafe"])
def test_saat_guvensizken_on_saniye_hic_zaman_yazilmaz(saha, saat_korumasi, durum):
    if durum == "unsafe":
        saat_korumasi.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert saat_korumasi.clock_state == durum

    okuyucu, cihaz_cfg, cihaz, proxy, _ = saha()
    assert okuyucu._masters[KOD].time_sync == "none", "guvensiz saatte master zaman gorevli kuruldu"
    assert _bekle(lambda: _bagli(okuyucu, cihaz_cfg), 20.0), "on kosul: cihaz baglanmali"
    assert _bekle(lambda: _cihaz_saat_istedi(okuyucu, cihaz_cfg), 10.0), (
        "on kosul: cihaz NEED_TIME bildirmeli (yoksa 'yazilmadi' anlamsiz olur)"
    )

    time.sleep(10.0)
    istekler = proxy.gw_istekleri()
    assert _g50v1_degerleri(istekler) == [], "saat guvensizken cihaza G50V1 YAZILDI"
    assert FC_DELAY_MEASURE not in [_fc(r) for r in istekler]
    assert cihaz.uygulama.yazilanlar == []
    assert cihaz.uygulama.need_time is True


# ==========================================================================
# KABUL 3 — koruma sonradan `safe`: master yeniden kurulur, dogru zaman yazilir
# ==========================================================================


def test_saat_sonradan_guvenli_olunca_dogru_zaman_yazilir(saha, saat_korumasi, caplog):
    okuyucu, cihaz_cfg, cihaz, proxy, _ = saha()
    assert _bekle(lambda: _cihaz_saat_istedi(okuyucu, cihaz_cfg), 20.0)
    time.sleep(1.0)
    assert cihaz.uygulama.yazilanlar == [], "dogrulanmamis saatte yazildi"

    with caplog.at_level(logging.INFO, logger=ym.logger.name):
        saat_korumasi.observe_http_date(_date())  # backend'den ilk Date

        # Poller'in yaptigi gibi: her turda read_device -> _ensure_master.
        def _tur():
            okuyucu.read_device(device=cihaz_cfg, signals=SINYALLER)
            return cihaz.uygulama.yazilanlar

        assert _bekle(_tur, 30.0), "saat guvenli olduktan sonra zaman yazilmadi"

    assert okuyucu._masters[KOD].time_sync == "nonlan"
    assert any("dnp3_time_sync_master_rebuild" in r.getMessage() for r in caplog.records)
    g50v1 = _g50v1_degerleri(proxy.gw_istekleri())
    assert len(g50v1) == 1
    assert abs(g50v1[0] - cihaz.uygulama.alis_anlari[0]) <= 2000


# ==========================================================================
# Smart oturumu acikken ACILIS ertelenir, KAPATMA ertelenmez (birim)
# ==========================================================================


class _SahteCache:
    def __init__(self, bagli: bool, kanit_yasi: float = 1.0) -> None:
        self._bagli = bagli
        self._kanit_at = time.monotonic() - kanit_yasi

    def is_connected(self) -> bool:
        return self._bagli

    def last_evidence_at(self) -> float:
        return self._kanit_at if self._bagli else 0.0

    def link_age(self) -> float:
        return 5.0 if self._bagli else -1.0


class _SahteMaster:
    def __init__(self, imza: tuple, *, policy: str, bagli: bool, kanit_yasi: float = 1.0) -> None:
        self.connection_fingerprint = imza
        self.session_policy = policy
        self.cache = _SahteCache(bagli, kanit_yasi)


@pytest.mark.parametrize(
    ("policy", "bagli", "yeni", "beklenen"),
    [
        ("smart", True, "nonlan", True),  # acik Smart oturumu: acilis bekler
        ("smart", False, "nonlan", False),  # oturum kapali: hemen kur
        ("continuous", True, "nonlan", False),  # continuous hep bagli: beklemez
        ("smart", True, "none", False),  # KAPATMA (saat guvensiz) asla beklemez
    ],
)
def test_zaman_acilisi_yalnizca_acik_smart_oturumunda_ertelenir(policy, bagli, yeni, beklenen):
    eski_imza = (
        "127.0.0.1",
        20000,
        10,
        1,
        "initiating",
        20100,
        policy,
        "none" if yeni != "none" else "nonlan",
    )
    yeni_imza = eski_imza[:-1] + (yeni,)
    mm = _SahteMaster(eski_imza, policy=policy, bagli=bagli)
    assert ym.Yadnp3TelemetryReader._zaman_senkronu_acilisi_ertelenmeli(mm, yeni_imza) is beklenen


def test_kanitsiz_bagli_smart_oturumu_acilisi_suresiz_ertelemez():
    """Yari-acik soket `is_connected()=True` der; erteleme SONSUZA uzamamali."""
    eski = ("127.0.0.1", 20000, 10, 1, "initiating", 20100, "smart", "none")
    yeni = eski[:-1] + ("nonlan",)
    mm = _SahteMaster(eski, policy="smart", bagli=True, kanit_yasi=ym._ZAMAN_ACILIS_ERTELEME_MAX_SEC + 30)
    assert ym.Yadnp3TelemetryReader._zaman_senkronu_acilisi_ertelenmeli(mm, yeni) is False


def test_baska_parametre_degisirse_erteleme_yok():
    eski = ("127.0.0.1", 20000, 10, 1, "initiating", 20100, "smart", "none")
    yeni = ("127.0.0.2", 20000, 10, 1, "initiating", 20100, "smart", "nonlan")
    mm = _SahteMaster(eski, policy="smart", bagli=True)
    assert ym.Yadnp3TelemetryReader._zaman_senkronu_acilisi_ertelenmeli(mm, yeni) is False


# ==========================================================================
# IIN1.4 gorunurlugu — GERCEK binding nesnesiyle
# ==========================================================================


@pytest.mark.parametrize(
    ("bit", "adlar"),
    [
        ("NEED_TIME", ("needTime", "NEED_TIME")),
        ("DEVICE_RESTART", ("deviceRestart", "DEVICE_RESTART")),
        ("EVENT_BUFFER_OVERFLOW", ("eventBufferOverflow", "EVENT_BUFFER_OVERFLOW")),
    ],
)
def test_iin_bayragi_gercek_iinfield_ile_okunur(bit, adlar):
    """1.15.1: `_iin_bit` bu binding'de HER ZAMAN False donuyordu."""
    alan = opendnp3.IINField()
    assert ym._iin_bit(alan, *adlar) is False
    alan.SetBit(getattr(opendnp3.IINBit, bit))
    assert ym._iin_bit(alan, *adlar) is True
    # Diger bayraklar etkilenmez.
    for diger in ("NEED_TIME", "DEVICE_RESTART", "EVENT_BUFFER_OVERFLOW"):
        if diger != bit:
            assert ym._iin_bit(alan, diger) is False


# ==========================================================================
# Yapilandirma: `lan` hala secilebilir; Horstmann'da UYARI
# ==========================================================================


def test_lan_acikca_secilebilir_ve_horstmann_profilinde_uyarir(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GATEWAY_MODE", "dnp3")
    monkeypatch.setenv("GATEWAY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DNP3_TIME_SYNC", "lan")
    okuyucu = build_adapter(Settings(_env_file=None))  # type: ignore[call-arg]
    geri = _korumayi_bagla(okuyucu, None)
    try:
        cihaz_cfg = DeviceConfig(
            code=KOD,
            name="sn2",
            ip_address="127.0.0.1",
            dnp3_address=10,
            ip_endpoint_type="initiating",
            master_ip_port=_bos_port(),
            signal_profile="horstmann_sn_2_0",
        )
        with caplog.at_level(logging.WARNING, logger=ym.logger.name):
            okuyucu.read_device(device=cihaz_cfg, signals=SINYALLER)
        assert okuyucu._masters[KOD].time_sync == "lan", "acik `lan` secimi uygulanmadi"
        assert any("dnp3_time_sync_lan_on_horstmann" in r.getMessage() for r in caplog.records)

        # Horstmann OLMAYAN profilde uyari yok.
        caplog.clear()
        diger = replace(cihaz_cfg, code="BASKA-1", signal_profile="default", master_ip_port=_bos_port())
        with caplog.at_level(logging.WARNING, logger=ym.logger.name):
            okuyucu.read_device(device=diger, signals=SINYALLER)
        assert not any("dnp3_time_sync_lan_on_horstmann" in r.getMessage() for r in caplog.records)
    finally:
        okuyucu.close()
        geri()


# ==========================================================================
# KABUL 1.17.0 — cihaz saat ISTEMESE de oturum basinda dogru UTC yazilir
# ==========================================================================


def _yokla(okuyucu, cihaz_cfg):
    """Gateway'in ana dongusunu taklit eder: `read_device` periyodik cagrilir.

    Oturum basi saat yazimi bu donguden (oturum dogrulandiginda) tetiklenir;
    tek bir kurulum cagrisi yetmez. Donen olay set edilince dongu durur.
    """
    dur = threading.Event()

    def dongu():
        while not dur.is_set():
            try:
                okuyucu.read_device(device=cihaz_cfg, signals=SINYALLER)
            except Exception:  # noqa: BLE001
                pass
            dur.wait(0.5)

    threading.Thread(target=dongu, daemon=True).start()
    return dur


@pytest.mark.parametrize("policy", ["continuous", "smart"])
def test_saat_istemeyen_cihaza_oturum_basinda_dogru_utc_yazilir(saha, saat_korumasi, caplog, policy):
    """Saati bozuk (+39 yil) ama IIN1.4 bildirmeyen Horstmann: kutuphanenin
    gorevi hic calismadigi icin saat duzelmiyordu. Oturum dogrulaninca tek bir
    G50V1 WRITE gitmeli; FC=23 (gecikme olcumu) GITMEZ."""
    saat_korumasi.observe_http_date(_date())
    assert saat_korumasi.clock_state == "safe"
    with caplog.at_level(logging.INFO, logger=ym.logger.name):
        okuyucu, cihaz_cfg, cihaz, proxy, _ = saha(policy=policy, saat_ister=False)
        assert cihaz.uygulama.need_time is False
        dur = _yokla(okuyucu, cihaz_cfg)
        try:
            assert _bekle(lambda: cihaz.uygulama.yazilanlar, 30.0), (
                "saat istemeyen cihaza oturum basinda saat YAZILMADI"
            )
            time.sleep(3.0)  # ayni oturumda IKINCI yazim olmamali
        finally:
            dur.set()

    istekler = proxy.gw_istekleri()
    g50v1 = _g50v1_degerleri(istekler)
    assert len(g50v1) == 1, f"tam bir G50V1 yazmasi bekleniyordu: {g50v1}"
    assert FC_DELAY_MEASURE not in [_fc(r) for r in istekler]
    assert g50v1[0] == cihaz.uygulama.yazilanlar[0]
    assert abs(g50v1[0] - cihaz.uygulama.alis_anlari[0]) <= 2000, (
        f"yazilan {ym._iso_utc(g50v1[0])} gercek UTC degil"
    )
    mesajlar = [r.getMessage() for r in caplog.records]
    assert any("dnp3_time_write_session" in m and f"device={KOD}" in m for m in mesajlar)


def test_saat_istemeyen_cihaz_ayar_kapaliyken_yazilmaz(saha, saat_korumasi):
    saat_korumasi.observe_http_date(_date())
    okuyucu, cihaz_cfg, cihaz, proxy, _ = saha(saat_ister=False, her_oturum=False)
    dur = _yokla(okuyucu, cihaz_cfg)
    try:
        assert _bekle(lambda: _bagli(okuyucu, cihaz_cfg), 20.0), "on kosul: cihaz baglanmali"
        time.sleep(6.0)
    finally:
        dur.set()
    assert _g50v1_degerleri(proxy.gw_istekleri()) == []
    assert cihaz.uygulama.yazilanlar == []


@pytest.mark.parametrize("durum", ["unknown", "unsafe"])
def test_saat_istemeyen_cihaza_saat_guvensizken_yazilmaz(saha, saat_korumasi, durum):
    if durum == "unsafe":
        saat_korumasi.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert saat_korumasi.clock_state == durum
    okuyucu, cihaz_cfg, cihaz, proxy, _ = saha(saat_ister=False)
    dur = _yokla(okuyucu, cihaz_cfg)
    try:
        assert _bekle(lambda: _bagli(okuyucu, cihaz_cfg), 20.0), "on kosul: cihaz baglanmali"
        time.sleep(8.0)
    finally:
        dur.set()
    assert _g50v1_degerleri(proxy.gw_istekleri()) == [], "saat guvensizken G50V1 YAZILDI"
    assert cihaz.uygulama.yazilanlar == []
