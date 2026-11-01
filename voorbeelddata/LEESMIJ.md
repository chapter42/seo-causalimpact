# Voorbeelddata

Een verzonnen testcase om de app mee te leren kennen: 400 url's van `www.voorbeeldwinkel.nl` (`/p/`, `/l/`, `/inf/`), met klikken, vertoningen en posities per url per dag. Nagemaakt met `scripts/maak_voorbeelddata.py`.

| Periode | Datums |
|---|---|
| Pre-periode | 1 juni t/m 30 augustus 2026 (13 weken) |
| Livegang | 31 augustus 2026 |
| Uitrol klaar, meten vanaf | 7 september 2026 |
| Einde data | 25 oktober 2026 (7 weken meten) |

## Bestanden

| Bestand | Gebruik |
|---|---|
| `stap1_pre_periode.csv` | Stap 1, bron *BigQuery*: upload als resultaat van de query (url × dag). |
| `indeling.csv` | Stap 2: de groepsindeling zoals stap 1 hem maakt (198 controle, 198 test). |
| `testconfig.json` | Stap 2: filters, zekerheid (90%) en looptijdadvies (3 weken bij een verwacht effect van 5%). |
| `stap2_groepstotalen_met_effect.csv` | Stap 2, bron *BigQuery*: wat de query van stap 2 teruggeeft. De testgroep heeft vanaf 7 september **+6%** klikken. |
| `stap2_groepstotalen_zonder_effect.csv` | Idem, maar zonder effect. |
| `stap2_url_data_met_effect.csv` | Stap 2, bron *Eigen CSV*: dezelfde data op url-niveau; de app koppelt zelf de groepen. |
| `stap2_url_data_zonder_effect.csv` | Idem, zonder effect. |

## Zo probeer je het

**Stap 1: groepen maken**

1. Bron *BigQuery (query + plakken)*. De query die de app toont kun je negeren.
2. Pre-periode: 01-06-2026 t/m 30-08-2026.
3. Paginatypes:
   ```
   pdp = /p/
   lijst = /l/
   info = /inf/
   ```
4. Upload `stap1_pre_periode.csv` en klik *Inlezen*, daarna *Maak groepen*.

Met seed 42 en de standaardinstellingen krijg je dezelfde indeling als `indeling.csv`. Bekijk de toetsen en het looptijdadvies.

**Stap 2: effect meten**

1. Zet *Gebruik de indeling uit stap 1* uit, en upload `indeling.csv` en `testconfig.json`.
2. Bron *BigQuery (query + plakken)*: upload `stap2_groepstotalen_met_effect.csv` en klik *Inlezen*.
3. Livegang: 31-08-2026. Meten vanaf: 07-09-2026.
4. Klik *Analyse draaien*.

Verwacht resultaat: een effect van ongeveer +5,5% (bandbreedte ongeveer +4% tot +7%), significant bij 90% zekerheid. De placebotest vindt niets.

Doe daarna hetzelfde met `stap2_groepstotalen_zonder_effect.csv`: dan vindt de app geen aantoonbaar effect (ongeveer −0,7%, bandbreedte rond nul).

**Tussenstand proberen:** zet *Meten vanaf* op 07-09-2026, en kies bij *Analyseperiode: eind* 20-09-2026 (2 weken meten). De app toont dan *Tussenstand: dag 14 van 21*.
