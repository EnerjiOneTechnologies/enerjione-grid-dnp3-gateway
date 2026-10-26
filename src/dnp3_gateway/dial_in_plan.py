"""Dial-In plani — son temastan sonra beklenen ILK planli rapora kalan sure.

NEDEN (1.18.0)
--------------
Horstmann kilavuzu (Smart Navigator 2.0, 104101-2038V2, 4.4.1): "Enable
Dial-In Settings" acikken cihaz her gun Dial-In Hour (UTC):Minute aninda ve
oradan Dial-In Interval adimlariyla arar — SABIT bir izgara. Plan disi
uyanmalar (Wake-up bayrakli bir nokta degisince) izgarayi KAYDIRMAZ.

1.17.0'a kadar beklenen rapor "son temas + aralik" idi. Plan disi bir
uyanmadan sonra (orn. 08:25) bu, 11:25'i bekletiyordu; cihaz ise 10:00'da
(izgara) arayacakti. Yanlis alarm uretmez ama 10:00 araması KACARSA gecikme
ancak 11:25'te fark edilirdi. Izgara biliniyorsa (`dial_in_anchor_min_utc`)
beklenen an izgaradaki bir sonraki centiktir.

SURE OLARAK DONER, AN OLARAK DEGIL: cagiran taraf bunu MONOTONIK sessizlik
yasiyla kiyaslar. Duvar saati yalnizca izgaradaki yeri bulmak icin bir kez
kullanilir; tek bir NTP sicramasi tum filoyu "gecikmis" gostermez.
"""

from __future__ import annotations

import math

#: Son temas bir plan aninin bu kadar ONCESINDE ise o planli rapor sayilir
#: (cihaz saati birkac saniye/dakika ileride olabilir).
ERKEN_SN = 300
#: Son temas bir plan aninin bu kadar SONRASINDA ise yine o planli rapordur
#: (modem kaydi + TCP kurulumu + gecikmeli arama).
GEC_SN = 900

GUN_SN = 86_400


def plana_kalan_sure(
    son_temas_epoch: float,
    aralik_sn: int,
    capa_sn: int | None,
    *,
    erken_sn: int = ERKEN_SN,
    gec_sn: int = GEC_SN,
) -> float:
    """Son temastan beklenen ilk planli rapora kadar gecen sure (saniye).

    `capa_sn` = izgaranin UTC gun icindeki baslangici (saniye, 0..86399).
    None ise izgara bilinmiyor demektir ve sonuc 1.17.0 ile aynidir:
    `aralik_sn` (son temas + aralik).

    Son temas bir plan anina denk geliyorsa (`-erken_sn .. +gec_sn`) o
    raporun kendisidir ve bir sonraki centik beklenir; degilse (plan disi
    uyanma) izgaradaki ILK sonraki centik beklenir.
    """
    if capa_sn is None or aralik_sn <= 0:
        return float(aralik_sn)
    faz = capa_sn % aralik_sn
    # Son temasa en yakin plan ani.
    k = round((son_temas_epoch - faz) / aralik_sn)
    en_yakin = faz + k * aralik_sn
    fark = son_temas_epoch - en_yakin
    if -erken_sn <= fark <= gec_sn:
        sonraki = en_yakin + aralik_sn
    else:
        sonraki = faz + (math.floor((son_temas_epoch - faz) / aralik_sn) + 1) * aralik_sn
    return max(0.0, sonraki - son_temas_epoch)
