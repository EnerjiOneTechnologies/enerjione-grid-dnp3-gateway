# yadnp3 upstream notu — `IMasterApplication.Now()` sessizce 0 yaziyor

Surum: `yadnp3==3.2.1.1` (Python binding). Gateway tarafindaki duzeltme
1.16.0'dadir; bu not kutuphaneye bildirilmek icindir.

## Sorun

1. `python/src/master.cpp` `PyMasterApplication::Now()` donus tipini
   `UTCTimestamp` olarak tanimlar. Python override'i baska bir tip (orn.
   `opendnp3.DNPTime`) donerse `pybind11::cast_error` olusur. Bu bir
   `std::exception` oldugu icin `catch (const std::exception&) {}` blogunda
   sessizce yutulur ve `UTCTimestamp(0)` donulur. Python tarafinda hicbir
   iz kalmaz.
2. Override icindeki her Python istisnasi da ayni sekilde 0'a donusur
   (`discard_as_unraisable` + `UTCTimestamp(0)`).
3. `SerialTimeSyncTask::BuildRequest` ve `LANTimeSyncTask::BuildRequest`
   `Now()` degerini dogrulamadan G50V1 / G50V3 olarak yazar. 0 donerse
   outstation'a 1970-01-01 00:00:00.000 UTC gider.
4. `python/src/types.cpp`: `DNPTime` ile `UTCTimestamp` arasinda
   `py::implicitly_convertible` yok.

## Etki

Sahada Horstmann Smart Navigator 2.0 cihazlarinin saati 1970'e (gun ici
00:00) ayarlandi ve Dial-In zamanlamasi bozuldu.

## Oneriler

- `Now()` cast hatasini yutmak yerine loglasin ya da `DNPTime` ->
  `UTCTimestamp` icin `implicitly_convertible` eklensin.
- Zaman gorevleri gecersiz bir zaman (0 ya da makul bir alt sinirin altinda)
  gelirse yazmak yerine gorevi basarisiz saysin.
- Uygulamanin zaman senkronunu calisma aninda acip kapatabilmesi icin bir
  kanca (`IMasterApplication`te `IsTimeSyncAllowed()` gibi) eklensin. Bugun
  tek yol master'i `timeSyncMode=None` ile yeniden kurmaktir.

Yeniden uretim betigi: `DNP3_Time_Sync/tools/yadnp3_now_repro.py`.
