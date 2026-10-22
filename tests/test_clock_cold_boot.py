"""ClockGuard soguk acilis fail-safe (A) — DNP3 saat yazma kapisi.

KAPATILAN ACIK
--------------
`is_safe_for_time_sync` hic olcum yapilmadiysa `True` donuyordu; gerekcesi
"backend'e hic ulasilamamis olabilir; eski davranisi bozmayalim" idi. Bu,
korumanin en cok gerektigi ani tam olarak acikta birakiyordu:

    host acilir       -> RTC/NTP henuz duzelmemis
    backend ULASILMAZ -> Date gozlemi YOK -> guard "guvenli" der
    onbellekli config -> DNP3 ayaga kalkar
    DNP3_TIME_SYNC=lan -> DOGRULANMAMIS host saati outstation'lara YAZILIR

Yani "olcemedim" durumu en riskliyken en gevsek karari veriyordu. Yanlis
saati 300 cihaza yazmak, hic yazmamaktan cok daha kotudur: cihazin kendi
olay tamponu da bozulur ve baska bir master'in okudugu damgalar da yanlis
olur.

KAPSAM SINIRI — BU TESTLERIN ASIL DERDI
---------------------------------------
Fail-safe YALNIZCA saat yazmayi kapatmali. Boot, onbellekli config, DNP3
polling, telemetri, NATS/outbox, health ve komut akisi (F1-F7) calismaya
DEVAM ETMELI. Asagida hem kapinin kapandigi hem de baska hicbir seyin
kapanmadigi olculuyor.

1.16.0 DUZELTMESI
-----------------
Bu dosyanin eski 7-8. testleri kapiyi `Now()`un "gecersiz zaman" (DNPTime(0))
dondurmesiyle olcuyordu ve YESILDI — oysa kutuphane o degeri kontrol etmeden
yaziyordu: cihaza 1970-01-01 gidiyordu. Kapi artik master KURULURKEN
(`_etkin_zaman_senkronu` -> `timeSyncMode=None`) uygulanir; `Now()` her
kosulda gecerli bir `UTCTimestamp` doner. Tel seviyesindeki kanit:
tests/test_zaman_senkronu_tel.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import pytest

from dnp3_gateway.resource_guard import CLOCK_UNSAFE_SKEW_SEC, ClockGuard


def _date(offset_sec: float = 0.0) -> str:
    """Backend `Date` basligi; pozitif offset = backend ILERIDE."""
    return format_datetime(datetime.now(timezone.utc) + timedelta(seconds=offset_sec))


# ==========================================================================
# 1-6. ClockGuard karar tablosu
# ==========================================================================


def test_1_hic_gozlem_yoksa_saat_yazma_engelli():
    """SOGUK ACILIS: olcum yoksa fail-safe."""
    g = ClockGuard()
    assert g.clock_state == "unknown"
    assert g.is_safe_for_time_sync is False


def test_2_gecerli_date_kucuk_sapma_izinli():
    g = ClockGuard()
    g.observe_http_date(_date())
    assert g.clock_state == "safe"
    assert g.is_safe_for_time_sync is True


def test_3_gecerli_date_guvensiz_sapma_engelli():
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert g.clock_state == "unsafe"
    assert g.is_safe_for_time_sync is False


def test_4_guvensizden_guvenliye_toparlanma():
    """Saat duzelirse kapi yeniden ACILMALI — kalici kilit degil."""
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert g.is_safe_for_time_sync is False

    g.observe_http_date(_date())
    assert g.clock_state == "safe"
    assert g.is_safe_for_time_sync is True


@pytest.mark.parametrize("eksik", [None, "", "   "])
def test_5_date_yoksa_unknown_kalir(eksik: Any):
    """Header hic gelmedi -> "olctum ve guvenli" DEMEK DEGIL."""
    g = ClockGuard()
    g.observe_http_date(eksik)
    assert g.clock_state == "unknown"
    assert g.is_safe_for_time_sync is False


@pytest.mark.parametrize("bozuk", ["bu bir tarih degil", "Mon, 32 Xxx 2026", "1755500000", "-"])
def test_6_bozuk_date_unknown_kalir(bozuk: str):
    g = ClockGuard()
    g.observe_http_date(bozuk)
    assert g.clock_state == "unknown"
    assert g.is_safe_for_time_sync is False


def test_histerezis_esik_civarinda_gidip_gelmez():
    """1.16.0: `unsafe` -> `safe` icin sapma 25 sn'nin altina inmeli.

    Zaman senkronu master kurulurken uygulandigi icin her `safe`/`unsafe`
    gecisi tum master'lari yeniden kurar; 30 sn civarinda saniye
    cozunurluklu `Date` olcumu gidip gelirse bu bir yeniden kurulum
    firtinasi olurdu.
    """
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert g.clock_state == "unsafe"
    g.observe_http_date(_date(-28))  # esigin altinda ama toparlanma esiginin ustunde
    assert g.clock_state == "unsafe", "esik civarinda unsafe -> safe gidip geliyor"
    g.observe_http_date(_date(-10))
    assert g.clock_state == "safe"


def test_histerezis_guvenli_baslangici_bozmaz():
    """Hic `unsafe` olmamis bir saat 25-30 sn bandinda `safe` kalir."""
    g = ClockGuard()
    g.observe_http_date(_date(-28))
    assert g.clock_state == "safe"


def test_bozuk_date_onceki_gecerli_olcumu_silmez():
    """Gecerli bir olcumden sonra bozuk bir header gelirse geri gidilmemeli."""
    g = ClockGuard()
    g.observe_http_date(_date())
    assert g.is_safe_for_time_sync is True

    g.observe_http_date("bu bir tarih degil")
    assert g.clock_state == "safe", "bozuk header gecerli olcumu sildi"


def test_snapshot_unknown_ile_unsafe_i_ayirt_ediyor():
    """Operator "neden yazilmiyor" sorusunu tek bakista cevaplayabilmeli."""
    g = ClockGuard()
    assert g.snapshot()["clock_state"] == "unknown"
    assert g.snapshot()["safe_for_time_sync"] is False
    assert g.snapshot()["skew_sec"] is None

    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    assert g.snapshot()["clock_state"] == "unsafe"
    assert g.snapshot()["skew_sec"] is not None


# ==========================================================================
# 7-8. SAAT YAZMA KAPISI — master KURULUMUNDA (`_etkin_zaman_senkronu`)
# ==========================================================================
#
# Asagisi ClockGuard'i degil, ADAPTER'in saat yazma kapisini olcer: guard
# dogru karar verse bile adapter onu yok sayarsa saha yine yanlis saat alir.
# Kapi `Now()` DEGILDIR: kutuphane `Now()`un dondurdugunu kontrol etmeden
# yazar. Kapi, master'in `timeSyncMode`unu belirleyen karardir.

opendnp3 = pytest.importorskip("opendnp3", reason="yadnp3 kurulu degil")

import time  # noqa: E402

from dnp3_gateway.adapters import dnp3_yadnp3_master as ym  # noqa: E402


@pytest.fixture
def guard_bagli():
    """Adapter'in modul-duzeyi guard referansini test suresince degistirir."""
    onceki = ym._clock_guard_ref.get("guard")
    try:
        yield ym.set_clock_guard
    finally:
        ym._clock_guard_ref["guard"] = onceki


def _master_app() -> Any:
    """GERCEK `_make_master_app` fabrikasi (gateway'in uretim yolu)."""
    return ym._make_master_app(ym._DeviceCache(), "SN2_0")


def _ms(ts: Any) -> int:
    """`Now()` donusu GERCEK bir `UTCTimestamp` olmali; degilse binding 0 yazar."""
    assert isinstance(ts, opendnp3.UTCTimestamp), (
        f"Now() {type(ts).__name__} dondu — binding yalnizca UTCTimestamp kabul eder, "
        "baska her tip sessizce UTCTimestamp(0) = 1970-01-01 olarak YAZILIR"
    )
    return int(ts.msSinceEpoch)


def test_7_soguk_acilista_master_zaman_gorevsiz_kurulur(guard_bagli):
    """Backend erisilemez + onbellekli config -> master `timeSyncMode=None`."""
    guard_bagli(ClockGuard())  # hic gozlem yok = soguk acilis
    assert ym._etkin_zaman_senkronu("nonlan") == ("none", "unknown")
    assert ym._etkin_zaman_senkronu("lan") == ("none", "unknown")


def test_8_backend_gelince_zaman_senkronu_aciliyor(guard_bagli):
    """Ilk gecerli Date gozleminden sonra yapilandirilan prosedur uygulanir."""
    g = ClockGuard()
    guard_bagli(g)
    assert ym._etkin_zaman_senkronu("nonlan")[0] == "none"  # once: unknown

    g.observe_http_date(_date())  # backend geldi
    assert ym._etkin_zaman_senkronu("nonlan") == ("nonlan", "safe")
    assert ym._etkin_zaman_senkronu("lan") == ("lan", "safe")


def test_guvensiz_sapmada_master_zaman_gorevsiz_kurulur(guard_bagli):
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))
    guard_bagli(g)
    assert ym._etkin_zaman_senkronu("nonlan") == ("none", "unsafe")


def test_none_yapilandirmasi_saatten_bagimsiz_kapali(guard_bagli):
    g = ClockGuard()
    g.observe_http_date(_date())
    guard_bagli(g)
    assert ym._etkin_zaman_senkronu("none")[0] == "none"


def test_guard_istisna_atarsa_kapi_kapanir(guard_bagli):
    class _Bozuk:
        @property
        def clock_state(self):
            raise RuntimeError("bozuk")

        @property
        def is_safe_for_time_sync(self):
            raise RuntimeError("bozuk")

    guard_bagli(_Bozuk())
    assert ym._etkin_zaman_senkronu("nonlan")[0] == "none"


# ==========================================================================
# Now() — HER KOSULDA gecerli UTCTimestamp (1.15.1 kok neden regresyonu)
# ==========================================================================


@pytest.mark.parametrize("durum", ["yok", "unknown", "safe"])
def test_now_her_durumda_gercek_utc_timestamp_doner(guard_bagli, durum):
    """1.15.1: `DNPTime` donuyordu -> binding cast edemedi -> cihaza 0 yazildi."""
    if durum != "yok":
        g = ClockGuard()
        if durum == "safe":
            g.observe_http_date(_date())
        guard_bagli(g)
    else:
        guard_bagli(None)
    deger = _ms(_master_app().Now())
    assert abs(deger - time.time() * 1000) <= 2000, deger


def test_now_guvensiz_sapmada_backend_duzeltilmis_zaman_doner(guard_bagli):
    """Kurulum ile yeniden kurulum arasindaki pencere icin: ASLA 0, sapmis host da degil."""
    g = ClockGuard()
    g.observe_http_date(_date(-(CLOCK_UNSAFE_SKEW_SEC + 60)))  # backend 90 sn GERIDE
    guard_bagli(g)
    deger = _ms(_master_app().Now())
    beklenen = (time.time() - g.skew_sec) * 1000
    assert abs(deger - beklenen) <= 2000


def test_now_kucuk_sapmada_bile_sapmis_host_saati_yazilmaz(guard_bagli):
    """Koruma 30 sn'ye kadar `safe` der; 10 sn sapmis host saati cihaza GITMEMELI.

    Degismez: cihaza yazilan deger gercek UTC'den en fazla 5 sn sapar.
    2 sn'nin ustundeki sapma backend saatine gore duzeltilir.
    """
    g = ClockGuard()
    g.observe_http_date(_date(-10))  # backend 10 sn GERIDE -> host 10 sn ileride
    guard_bagli(g)
    assert g.clock_state == "safe"
    deger = _ms(_master_app().Now())
    backend_ms = (time.time() - g.skew_sec) * 1000
    assert abs(deger - backend_ms) <= 2000
    assert abs(deger - time.time() * 1000) >= 8000, "sapmis host saati yazilacakti"


def test_now_guard_istisna_atsa_bile_istisna_sizdirmaz(guard_bagli):
    class _Bozuk:
        @property
        def clock_state(self):
            raise RuntimeError("bozuk")

    guard_bagli(_Bozuk())
    deger = _ms(_master_app().Now())
    assert deger >= ym._GECERLI_ZAMAN_ALT_SINIR_MS


def test_now_ic_hata_olsa_bile_istisna_sizdirmaz(guard_bagli, monkeypatch):
    """`Now()` icindeki her istisna binding'de 0'a donusur — disari SIZMAMALI."""
    guard_bagli(None)

    def _patla() -> tuple[int, str]:
        raise RuntimeError("beklenmeyen")

    monkeypatch.setattr(ym, "_saat_tahmini_ms", _patla)
    deger = _ms(_master_app().Now())
    assert abs(deger - time.time() * 1000) <= 2000


def _nonlan_dizisi(app: Any, tabanlar: list[int], monkeypatch) -> list[int]:
    """Kutuphanenin NonLAN cagri dizisini (start, gecikme, yazma) taklit eder."""
    kalan = list(tabanlar)
    monkeypatch.setattr(ym, "_saat_tahmini_ms", lambda: (kalan.pop(0), "host"))
    app.OnTaskStart(opendnp3.MasterTaskType.NON_LAN_TIME_SYNC, 0)
    return [_ms(app.Now()) for _ in tabanlar]


def test_now_nonlan_gecikme_telafisi_tolerans_disina_cikamaz(guard_bagli, monkeypatch):
    """1.15.1 yan riski: once 0 sonra gercek zaman -> gecikme ~now/2 -> ~28 yil ileri.

    Kutuphane `yazilan = 3. cagri + (2. - 1. - turnaround) / 2` hesaplar.
    Tur suresi toleransin iki katini asarsa 2. cagri 1.nin degerini almali
    (kutuphane gecikmeyi 0 hesaplar).
    """
    guard_bagli(None)
    t0 = int(time.time() * 1000)
    s = _nonlan_dizisi(_master_app(), [t0, t0 + 20_000, t0 + 20_010], monkeypatch)
    assert s[1] == s[0], "asiri tur suresi gecikme telafisine yansidi"
    assert s[2] == t0 + 20_010


def test_now_nonlan_normal_tur_suresi_degismeden_gecer(guard_bagli, monkeypatch):
    guard_bagli(None)
    t0 = int(time.time() * 1000)
    s = _nonlan_dizisi(_master_app(), [t0, t0 + 300, t0 + 310], monkeypatch)
    assert s == [t0, t0 + 300, t0 + 310]


def test_zaman_yazimi_info_loglanir(guard_bagli, monkeypatch, caplog):
    """Her yazim: device, ISO UTC deger ve yontem INFO'da."""
    guard_bagli(None)
    t0 = 1_792_661_796_000  # 2026-10-22T09:36:36Z
    with caplog.at_level("INFO", logger=ym.logger.name):
        _nonlan_dizisi(_master_app(), [t0, t0 + 200, t0 + 210], monkeypatch)
    satir = [r.getMessage() for r in caplog.records if "dnp3_time_sync_write" in r.getMessage()]
    assert len(satir) == 1, satir
    assert "device=SN2_0" in satir[0]
    assert "value=2026-10-22T09:36:36.310Z" in satir[0]
    assert "method=nonlan" in satir[0]


def test_gecersiz_zaman_yazimi_error_loglanir(guard_bagli, monkeypatch, caplog):
    """Host saati 2001 oncesindeyse (guard YOK) yazim engellenemez ama ERROR."""
    guard_bagli(None)
    with caplog.at_level("INFO", logger=ym.logger.name):
        _nonlan_dizisi(_master_app(), [1000, 1100, 1110], monkeypatch)
    assert any("dnp3_time_sync_invalid_value" in r.getMessage() for r in caplog.records)
    assert not any("dnp3_time_sync_write" in r.getMessage() for r in caplog.records)


# ==========================================================================
# KAPSAM SINIRI: fail-safe BASKA HICBIR SEYI kapatmiyor
# ==========================================================================


def test_saat_kapisi_yalnizca_saat_yazmayi_etkiliyor():
    """`is_safe_for_time_sync` TEK bir yerde tuketiliyor: `_etkin_zaman_senkronu`.

    Biri ilerde bu bayragi polling/telemetri/komut yoluna baglarsa, soguk
    acilista gateway'in YARISI kapanir. Bu test o regresyonu yakalar.
    """
    import pathlib

    kok = pathlib.Path(ym.__file__).parent.parent
    kullanim = []
    for yol in kok.rglob("*.py"):
        for no, satir in enumerate(yol.read_text(encoding="utf-8").splitlines(), 1):
            if "is_safe_for_time_sync" in satir and "def " not in satir:
                kullanim.append(f"{yol.relative_to(kok).as_posix()}:{no}")

    # resource_guard.py (snapshot) + adapter (Now kapisi) disinda kullanim OLMAMALI.
    beklenmeyen = [
        k for k in kullanim if not k.startswith(("resource_guard.py", "adapters/dnp3_yadnp3_master.py"))
    ]
    assert not beklenmeyen, f"saat bayragi baska yollara sizmis: {beklenmeyen}"
