# SEO-test met CausalImpact

Streamlit-app voor SEO-splittests op url-niveau, in twee stappen:

1. **Groepen maken** (vooraf): je kiest welke url's meedoen, de app haalt de pre-periode op en verdeelt de url's gestratificeerd over een controlegroep en een of meer testgroepen. Daarna toetst hij of de groepen gelijk genoeg zijn om later een effect te kunnen meten.
2. **Effect meten** (achteraf): de app leest de indeling in, haalt de data van pre-periode tot nu op en meet met CausalImpact hoeveel de testgroep afwijkt van wat de controlegroep voorspelt. Er draait standaard een placebotest mee.

Data komt uit de **GSC API** (serviceaccount) of uit de **BigQuery bulk export**. Bij BigQuery maakt de app de query, draai jij die zelf en plak je het resultaat terug (of upload je de CSV). Er worden nergens tabellen aangemaakt.

## Installeren

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Voor het Google-model (`tfcausalimpact`, zwaar vanwege TensorFlow):

```bash
pip install -r requirements-tf.txt
```

Zonder die stap werkt alles met het statsmodels-model.

## Inloggen voor de GSC API

Alleen nodig voor de bron *GSC API*. De BigQuery-route (query draaien, resultaat plakken) werkt zonder inloggen.

**Lokaal: gcloud-login (aanbevolen, geen sleutelbestanden)**

1. Eenmalig: installeer gcloud (`brew install --cask google-cloud-sdk`) en log in:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```
2. Per klant: maak een serviceaccount waar jij als mag handelen:
   ```bash
   bash scripts/nieuwe-klant.sh <gcp-project-id> <klantnaam>
   ```
   Het script zet de API's aan, maakt `seo-<klant>@<project>.iam.gserviceaccount.com`, geeft jou de rol *Service Account Token Creator* en zet de klant in `serviceaccounts.txt`.
3. Voeg dat serviceaccount in Search Console toe aan de property van de klant (recht: Beperkt).
4. In de app: 🔑 Inloggen → *gcloud-login* → kies de klant → *Verbinden*.

De app vraagt namens het serviceaccount een token aan dat een uur geldig is. Er staat nergens een sleutel op schijf.

**Zonder gcloud: JSON-key.** Kies *JSON-key* en upload de sleutel, of zet het pad in `GOOGLE_APPLICATION_CREDENTIALS`. De sleutel blijft alleen in het geheugen van de sessie.

## Stap 1 · Groepen maken

**Selectie.** Kies pre-periode, landen (ISO-3, zoals `nld`), apparaten, zoektype, url-patronen (regex, RE2) en paginatypes (`pdp = /p/`, één per regel). Paginatypes bepalen de strata; de patronen bepalen wie meedoet.

**Bron.**
- *BigQuery:* de app toont een `SELECT` op `searchdata_url_impression` (url × dag of week). Draai die, kopieer het resultaat (CSV, TSV of JSON) en plak het terug, of upload de download.
- *GSC API:* kies de property en klik Ophalen. De app vraagt per land × apparaat per maand op en telt op.

**Zo verdeelt de app:**
1. Per url: klikken, vertoningen, CTR, positie en trend (tweede helft tegen eerste helft van de pre-periode).
2. Uitsluiten: url's onder de klikdrempel en uitschieters (meer dan x% van alle klikken).
3. Strata: paginatype × verkeersklasse × trendklasse. Te kleine strata vallen terug op een grover niveau.
4. Binnen elk stratum sorteert de app op klikken, maakt blokjes van *k* url's (k = aantal groepen) en loot binnen elk blokje de groepen. Zo krijg je gematchte paren in plaats van puur toeval.

**Zo toetst de app (per testgroep tegen de controle):**

| Toets | Drempel (instelbaar) | Wat het zegt |
|---|---|---|
| SMD per kenmerk | \|SMD\| < 0,10 | Groepen lijken op elkaar in verkeer, CTR, positie en trend |
| Correlatie van de reeksen | ≥ 0,90 | De controle kan de testgroep voorspellen |
| Parallelle trend | p ≥ 0,20 | De verhouding test/controle loopt niet al weg |
| A/A-placebo | \|effect\| ≤ 3% | Op een nepdatum vind je niets |
| MDE (indicatief) | n.v.t. | Kleinste effect dat je met deze looptijd waarschijnlijk ziet |

Haalt een indeling de toetsen niet, dan probeert de app een nieuwe seed (standaard maximaal 50 keer). Lukt het niet, dan kiest hij de best scorende en zegt welke toets faalt.

**Export:** `indeling.csv` (url, groep, stratum en pre-cijfers), `testconfig.json` (alle filters en instellingen, bewaar deze bij de test) en de url's van de testgroep(en) om door te zetten naar development.

## Stap 2 · Effect meten

Upload `indeling.csv` en `testconfig.json`, of gebruik de indeling uit stap 1 in dezelfde sessie. De filters worden ingevuld vanuit de config.

**Bron.**
- *BigQuery:* de query bevat de indeling als lijst (`UNNEST`) en geeft alleen totalen per dag per groep terug. Plakken is dan klein. Boven ~1 MB querytekst (rond 50.000 url's) valt de app terug op een url-query en koppelt hij de groepen zelf.
- *GSC API:* ophalen en koppelen op url.
- *Eigen CSV:* url × datum, datum × groep of een brede tabel met een datumkolom en reeksen.

**Instellingen.**
- *Livegang* en *meten vanaf*: de dagen ertussen (uitrol, recrawl) blijven buiten de analyse. Een veelvoud van 7 dagen houdt het weekpatroon zuiver.
- *Controle*: normaal alleen `controle`. Andere testgroepen zijn zelf beïnvloed en horen hier niet.
- *Model*:
  - `statsmodels` (standaard): lokaal niveau plus de controle als regressor, met dezelfde kleine niveauvariantie als CausalImpact. Het is snel en stabiel. De intervallen zijn iets smaller omdat parameteronzekerheid niet meetelt.
  - `tfcausalimpact`: het Google-model. Het draait in een apart proces met een tijdslimiet en valt bij een deadlock terug op statsmodels.

**Uitkomst:** een conclusie in gewone taal, het relatieve en cumulatieve effect met bandbreedte, de p-waarde, de fit in de pre-periode, het driepanelenplot en de placebotest. Downloads: PNG, KPI's (JSON) en de reeksen (CSV).

## Tests

```bash
python -m pytest -q tests
```

De tests controleren onder meer dat een ingebouwd effect van +8% wordt teruggevonden (de test eist 5 tot 11%; in de praktijk kwam er 7,5 tot 8% uit), dat een situatie zonder effect niet significant is, dat geplakte NL-, TSV- en JSON-tabellen goed worden gelezen, en dat de app-flows werken.

## Bestanden

```
app.py                  Streamlit-app (stap 1 en 2)
core/grouping.py        stratificatie, toewijzing, balanstoetsen
core/impact.py          CausalImpact (statsmodels + tfcausalimpact), placebo, grafiek
core/_tfci_worker.py    tfcausalimpact in een apart proces
core/sql_builder.py     BigQuery-queries voor stap 1 en 2
core/gsc_api.py         Search Console API: gcloud-login of JSON-key
core/data_io.py         plakken/uploaden, normaliseren, NL-getalnotatie, demodata
scripts/nieuwe-klant.sh serviceaccount per klant, zonder key
```
