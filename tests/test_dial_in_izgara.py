"""1.18.0 — Dial-In IZGARASI: plan disi uyanma beklenen raporu KAYDIRMAZ.

Saha (2026-10-26): SN 2.0, Enable Dial-In Settings acik, 00:59 UTC + 180 dk
-> 00:59 / 03:59 / 06:59 / 09:59 ... Cihaz plan disi uyandiginda (Wake-up
bayrakli bir nokta degisince) sonraki arama yine izgaradadir. 1.17.0 "son
temas + aralik" bekliyordu: 08:25'teki plan disi uyanmadan sonra 11:25'i;
09:59 aramasi kacsa gecikme ancak 11:25+ fark edilirdi.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from dnp3_gateway.adapters import dnp3_yadnp3_master as mod
from dnp3_gateway.backend.config_client import _parse_optional_int
from dnp3_gateway.dial_in_plan import plana_kalan_sure

from .conftest import make_device

ARALIK = 180 * 60
CAPA = 59 * 60  # 00:59 UTC


def _an(saat: int, dk: int, sn: int = 0) -> float:
    return datetime(2026, 10, 26, saat, dk, sn, tzinfo=timezone.utc).timestamp()


def test_planli_rapordan_sonra_bir_sonraki_centik_beklenir():
    # 06:59:33'teki planli rapor -> sonraki 09:59
    kalan = plana_kalan_sure(_an(6, 59, 33), ARALIK, CAPA)
    assert _an(6, 59, 33) + kalan == _an(9, 59)


def test_plan_disi_uyanma_izgarayi_kaydirmaz():
    # 08:25 plan disi -> sonraki 09:59 (1.17.0: 11:25)
    kalan = plana_kalan_sure(_an(8, 25), ARALIK, CAPA)
    assert _an(8, 25) + kalan == _an(9, 59)
    assert kalan < ARALIK


def test_cihaz_saati_bir_kac_dakika_ileride_ise_yine_o_planli_rapor_sayilir():
    # Cihaz 2 dk erken aradi (06:57): bu 06:59 raporudur; sonraki 09:59,
    # 06:59 DEGIL (aksi halde 15 dk sonra sahte 'gecikti').
    kalan = plana_kalan_sure(_an(6, 57), ARALIK, CAPA)
    assert _an(6, 57) + kalan == _an(9, 59)


def test_gecikmeli_planli_rapor_da_o_centige_sayilir():
    # Modem kaydi uzun surdu: 07:10'da baglandi -> yine 06:59 raporu.
    kalan = plana_kalan_sure(_an(7, 10), ARALIK, CAPA)
    assert _an(7, 10) + kalan == _an(9, 59)


def test_izgara_bilinmiyorsa_117_davranisi():
    assert plana_kalan_sure(_an(8, 25), ARALIK, None) == ARALIK


def test_gunluk_aralikta_izgara():
    # 1440 dk, 03:54 UTC (fabrika saati) — gun sonunu dogru gecer.
    kalan = plana_kalan_sure(_an(23, 0), 1440 * 60, 3 * 3600 + 54 * 60)
    assert _an(23, 0) + kalan == datetime(2026, 10, 27, 3, 54, tzinfo=timezone.utc).timestamp()


def test_capa_alani_ayristirilir_ve_sinirlanir():
    assert _parse_optional_int({"dial_in_anchor_min_utc": 59}, "dial_in_anchor_min_utc", lo=0, hi=1439) == 59
    assert (
        _parse_optional_int({"dial_in_anchor_min_utc": 1440}, "dial_in_anchor_min_utc", lo=0, hi=1439) is None
    )
    assert _parse_optional_int({}, "dial_in_anchor_min_utc", lo=0, hi=1439) is None


def _okuyucu_ve_master(son_temas: float | None):
    okuyucu = object.__new__(mod.Yadnp3TelemetryReader)
    mm = SimpleNamespace(cache=SimpleNamespace(last_valid_contact_wall=lambda: son_temas))
    return okuyucu, mm


def test_adaptor_izgara_varsa_sonraki_centigi_bekler():
    okuyucu, mm = _okuyucu_ve_master(_an(8, 25))
    cihaz = replace(make_device("IZG-1"), dial_in_interval_min=180, dial_in_anchor_min_utc=59)
    assert okuyucu._beklenen_rapor_suresi(mm, cihaz, ARALIK) == _an(9, 59) - _an(8, 25)


def test_adaptor_izgara_yoksa_ya_da_temas_yoksa_aralik():
    okuyucu, mm = _okuyucu_ve_master(_an(8, 25))
    cihaz = replace(make_device("IZG-2"), dial_in_interval_min=180)
    assert okuyucu._beklenen_rapor_suresi(mm, cihaz, ARALIK) == ARALIK
    okuyucu, mm = _okuyucu_ve_master(None)
    cihaz = replace(make_device("IZG-3"), dial_in_interval_min=180, dial_in_anchor_min_utc=59)
    assert okuyucu._beklenen_rapor_suresi(mm, cihaz, ARALIK) == ARALIK


def test_onbellekten_gelen_gecersiz_capa_yok_sayilir():
    # Disk onbellegi parser'i atlar; 5000 dk gibi bir deger izgara sayilmaz.
    cihaz = replace(make_device("IZG-4"), dial_in_anchor_min_utc=5000)
    assert mod.Yadnp3TelemetryReader._dial_in_capa_sn(cihaz) is None
