# SEO-test met CausalImpact

Streamlit-app voor SEO-splittests op url-niveau. Je gebruikt hem twee keer per test:

1. **Vooraf: groepen maken.** Je kiest welke url's meedoen. De app haalt de pre-periode op uit Search Console of BigQuery, verdeelt de url's gestratificeerd over een controlegroep en een of meer testgroepen, en toetst of die groepen gelijk genoeg zijn om later een effect te kunnen meten.
2. **Achteraf: effect meten.** Je leest de indeling weer in, haalt de data van pre-periode tot nu op, en de app meet met CausalImpact hoeveel de testgroep afwijkt van wat de controlegroep voorspelt. Een placebotest controleert of de opzet deugt.

Er worden nergens tabellen aangemaakt en er wordt niets opgeslagen buiten je eigen downloads.

---

## Inhoud

1. [Snel starten](#snel-starten)
2. [Inloggen](#inloggen)
3. [Stap 1: groepen maken](#stap-1-groepen-maken)
4. [Wat je met de export doet](#wat-je-met-de-export-doet)
5. [Stap 2: effect meten](#stap-2-effect-meten)
6. [Het resultaat lezen](#het-resultaat-lezen)
7. [Geplakte en geüploade data](#geplakte-en-geüploade-data)
8. [Problemen oplossen](#problemen-oplossen)
9. [Online draaien (share.streamlit.io)](#online-draaien-sharestreamlitio)
10. [Hoe het werkt](#hoe-het-werkt)
11. [Ontwikkelen en testen](#ontwikkelen-en-testen)

---

## Snel starten

Eenmalig (macOS):

```bash
git clone https://github.com/chapter42/seo-causalimpact.git
cd seo-causalimpact
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Starten:

```bash
cd ~/seo-causalimpact && .venv/bin/python -m streamlit run app.py
```

De app opent op http://localhost:8501.

**Stoppen:** druk in de terminal waarin hij draait op **Ctrl + C**. Het browsertabblad sluiten stopt de app niet. Terminal kwijt? Gebruik `pkill -f streamlit`.

**Bijwerken:** stop de app, draai `git pull` en start opnieuw. Als `requirements.txt` veranderd is, ook nog `.venv/bin/python -m pip install -r requirements.txt`.

Gebruik steeds `.venv/bin/python -m ...`. Dan gebruiken pip en Streamlit gegarandeerd dezelfde Python, en heb je geen last van de Homebrew-melding *externally-managed-environment*.

Wil je eerst zien hoe het werkt? Kies in stap 1 en 2 als bron **Demodata**.

---

## Inloggen

Inloggen is alleen nodig voor de bron **GSC API** en om in de zijbalk je BigQuery-exports te laten vinden. De bron **BigQuery (query + plakken)** werkt zonder inloggen: je draait de query zelf in de BigQuery-console met je eigen account.

### Optie 1: gcloud-login met je eigen account (snelst)

Eenmalig in de terminal:

```bash
brew install --cask google-cloud-sdk        # als gcloud er nog niet staat
gcloud auth login
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/webmasters.readonly,https://www.googleapis.com/auth/cloud-platform
```

De laatste regel moet eindigen met *Credentials saved to file*. Google koppelt daarbij een quotaproject: het project waarop de API-aanroepen lopen. Zet daar de twee API's aan (eenmalig):

```bash
gcloud services enable searchconsole.googleapis.com bigquery.googleapis.com --project JOUW_QUOTAPROJECT
```

Ander quotaproject kiezen: `gcloud auth application-default set-quota-project JOUW_PROJECT`.

In de app: zijbalk → **🔑 Inloggen** → *gcloud-login* → Handelen als *(eigen gcloud-account)* → **Verbinden**.

### Optie 2: gcloud-login, handelen als serviceaccount per klant

Netter dan je eigen account: per klant een eigen serviceaccount, zonder sleutelbestand. De app vraagt namens dat account een token aan dat een uur geldig is.

1. Per klant, eenmalig:
   ```bash
   bash scripts/nieuwe-klant.sh <gcp-project-id> <klantnaam>
   ```
   Het script maakt het project aan als het nog niet bestaat, zet de API's aan (Search Console, IAM Credentials, BigQuery), maakt `seo-<klant>@<project>.iam.gserviceaccount.com`, geeft jou de rol *Service Account Token Creator* op dat account en zet de klant in `serviceaccounts.txt`. Dat bestand staat in `.gitignore`; een voorbeeld staat in `serviceaccounts.example.txt`.
2. Voeg het serviceaccount in Search Console toe aan de property van de klant: Instellingen → Gebruikers en rechten → Gebruiker toevoegen, recht *Beperkt*.
3. Voor BigQuery (optioneel): geef het account in het project van de klant de rol *BigQuery Job User*, en op de dataset met de GSC-export de rol *BigQuery Data Viewer*. Het script toont de commando's.
4. In de app: **🔑 Inloggen** → *gcloud-login* → Handelen als *<klant>* → **Verbinden**.

### Optie 3: JSON-key

Voor plekken zonder gcloud. Kies *JSON-key* en upload de sleutel, of vul het pad in. De sleutel blijft alleen in het geheugen van die sessie. Een sleutel is een wachtwoord: bewaar hem nooit in een repo (`credentials/` en `service-account*.json` staan in `.gitignore`).

### Na Verbinden: property en BigQuery-export kiezen

In de zijbalk verschijnen twee keuzelijsten:

- **Property**: alle Search Console-properties die het account ziet. `sc-domain:voorbeeld.nl` is een domeinproperty (alle subdomeinen, http en https; meestal de beste keus). `https://www.voorbeeld.nl/` is een url-prefix-property. Leeg? Dan is het account nog niet toegevoegd in Search Console.
- **BigQuery-export**: de app doorzoekt alle projecten die het account ziet op datasets met een tabel `searchdata_url_impression`, en toont per dataset het project, de naam en het aantal rijen. Dit zijn alleen metadata-aanroepen, zonder kosten. Bij veel projecten duurt het even; de voortgang staat onder de knop. Welke dataset bij welke property hoort, zie je in Search Console onder Instellingen → Bulkgegevensexport.

Stap 1 en 2 gebruiken deze keuzes vanzelf.

---

## Stap 1: groepen maken

### Bron kiezen

| Bron | Wat je doet | Nodig |
|---|---|---|
| **BigQuery (query + plakken)** | De app maakt een `SELECT` op `searchdata_url_impression`. Jij draait die in de BigQuery-console en plakt het resultaat terug, of uploadt de CSV. | Toegang tot de bulkexport in de console |
| **GSC API** | De app haalt de data op voor de property uit de zijbalk. | Inloggen (zie boven) |
| **Demodata** | 600 verzonnen url's om de app te leren kennen. | Niets |

Bij BigQuery vul je de tabel in (`project.dataset` of de volledige tabelnaam), of je kiest hem in de zijbalk. *Minimaal vertoningen per url in de query* (standaard 100) houdt de export klein: url's daaronder komen niet mee.

### Selectie

| Veld | Uitleg |
|---|---|
| Pre-periode | De periode waarop de groepen worden verdeeld en getoetst. Standaard de laatste 12 weken tot 3 dagen geleden. Neem minimaal 8 weken; een heel jaar als seizoen meespeelt. |
| Detailniveau | *Dag* geeft betere toetsen. *Week* maakt de export zo'n 7 keer kleiner. |
| Landen | ISO-3-codes in kleine letters, zoals `nld` en `bel`. Leeg betekent alle landen. |
| Apparaten | Leeg betekent alle apparaten. |
| Zoektype | Meestal `WEB`. |
| Url-patronen opnemen | Regex (RE2), één per regel. Een url doet mee als hij op minstens één patroon matcht. Voorbeeld: `/p/` |
| Url-patronen uitsluiten | Regex, één per regel. Voorbeeld: `\?` om url's met parameters uit te sluiten. |
| Paginatypes | `label = regex`, één per regel, eerste match telt. Bepaalt de strata. Voorbeeld: `pdp = /p/` en `lijst = /l/`. Wat nergens op matcht wordt `overig`. |

### Indeling

| Instelling | Standaard | Uitleg |
|---|---|---|
| Groepen | `controle, test` | De eerste is altijd de controle. Twee tot vier groepen; meer groepen betekent minder url's per groep. |
| Seed | 42 | Zelfde data en zelfde seed geven dezelfde indeling. |
| Verkeersklassen per paginatype | 4 | Hoe fijn op verkeer wordt gestratificeerd. |
| Stratificeer ook op trend | aan | Url's die stijgen, stabiel zijn of dalen worden eerlijk verdeeld. |
| Min. klikken per url | 10 | Url's met minder klikken in de pre-periode gaan eruit. Ze voegen vooral ruis toe. |
| Max. aandeel per url | 2% | Url's die meer dan dit deel van alle klikken halen zijn uitschieters en gaan eruit. Eén uitschieter in één groep kan een test kapotmaken. |
| Geplande looptijd | 42 dagen / 6 weken | Alleen voor de MDE-schatting (zie onder). |
| Max. pogingen | 50 | Hoe vaak de app opnieuw verdeelt als de toetsen niet slagen. |

### De toetsen

Per testgroep vergeleken met de controle. De drempels zijn aan te passen onder *Drempels voor de toetsen*.

| Toets | Drempel | Wat het zegt |
|---|---|---|
| SMD per kenmerk | \|SMD\| ≤ 0,10 | De groepen lijken op elkaar in klikken, vertoningen, CTR, positie en trend. SMD is het verschil gedeeld door de spreiding; onder 0,1 is gangbaar. |
| Correlatie van de reeksen | ≥ 0,90 | De controle beweegt mee met de testgroep en kan hem dus voorspellen. |
| Parallelle trend | p ≥ 0,20 | De verhouding test/controle loopt in de pre-periode niet al uit elkaar. |
| A/A-placebo | \|effect\| ≤ 3% | Doe je alsof de test op 70% van de pre-periode begon, dan vind je niets. |
| MDE (indicatief) | geen drempel | Het kleinste effect dat je met deze groepen en looptijd waarschijnlijk aantoont (80% power, gecorrigeerd voor autocorrelatie). Zie het als ondergrens. |

Haalt een indeling niet alles, dan verdeelt de app opnieuw met een volgende seed. Lukt het na het maximum aantal pogingen niet, dan kiest hij de best scorende indeling en meldt hij welke toets faalt. Lage correlatie betekent meestal te weinig verkeer per groep: verruim de scope of meet per week.

Onder de toetsen staan de groepen in de pre-periode als lijn (geïndexeerd op 100), de verdeling per stratum en de uitgesloten url's met de reden.

---

## Wat je met de export doet

| Download | Waarvoor |
|---|---|
| **Indeling, alle groepen (CSV)** | `url, groep, stratum, page_type` plus de pre-cijfers. Dit is de input voor stap 2. Bewaar hem. |
| **Testconfig (JSON)** | Alle filters, paginatypes, instellingen, de gebruikte seed en (bij de GSC API) de property. Stap 2 vult hiermee alles in. Bewaar hem bij de indeling. |
| **Een lijst per groep (TXT)** | Eén url per regel. De lijst van de **testgroep** gaat naar wie de wijziging doorvoert. De lijst van de **controle** is de groep die tijdens de test niet aangeraakt mag worden: geef hem mee, zodat niemand daar per ongeluk ook iets wijzigt of hem in een andere test gebruikt. |

Onder de downloads kun je elke lijst ook direct kopiëren.

Tijdens de test:

- Voer de wijziging alleen door op de testgroep, en noteer de datum waarop hij live ging en de datum waarop de uitrol klaar was.
- Laat de controle met rust. Wijzigingen aan sitebrede templates, interne links of navigatie raken ook de controle (spillover) en maken het resultaat onbetrouwbaar.
- Kies de controle nooit achteraf opnieuw. Dan zoek je, bewust of niet, de controle die het mooiste resultaat geeft.

---

## Stap 2: effect meten

### Indeling inlezen

Gebruik de indeling uit stap 1 (zelfde sessie), of upload `indeling.csv`. Upload ook `testconfig.json`: dan staan periode, landen, patronen en tabel meteen goed, en stelt de app de property van stap 1 voor.

### Bron kiezen

| Bron | Wat je doet |
|---|---|
| **BigQuery (query + plakken)** | De query bevat de groepsindeling als lijst en geeft alleen totalen per dag per groep terug (`date, groep, clicks, impressions, sum_position, n_urls`). Je plakt dus maar een paar honderd regels terug. Boven ongeveer 50.000 url's wordt de querytekst groter dan de BigQuery-limiet van 1 MB; dan valt de app terug op een url-query, en koppelt hij zelf de groepen. |
| **GSC API** | De app haalt url × dag op voor de property uit de zijbalk en koppelt de groepen. |
| **Eigen CSV** | Url × datum, datum × groep, of een brede tabel met een datumkolom en per reeks een kolom. Zie [Geplakte en geüploade data](#geplakte-en-geüploade-data). |
| **Demodata** | Werkt met de demo-indeling uit stap 1 en een ingebouwd effect dat je zelf kiest. Handig om te zien hoe een echt effect eruitziet. |

Bij url-data toont de app welk deel van de url's uit de indeling in de data is gevonden. Onder 80% krijg je een waarschuwing; dan klopt de property, het land of een patroon meestal niet.

### Instellingen

| Instelling | Uitleg |
|---|---|
| Metric | Klikken of vertoningen. Klikken zijn het meest zakelijk, vertoningen reageren sneller op rankingwijzigingen. |
| Testgroep | De groep met de wijziging. |
| Controle | Normaal alleen `controle`. Andere testgroepen zijn zelf beïnvloed en horen hier niet. |
| Livegang | De dag waarop de uitrol begon. Alles daarvoor is pre-periode. |
| Meten vanaf | De dag waarop de uitrol klaar was en Google de pagina's heeft gezien. De dagen tussen livegang en meten vanaf blijven buiten de analyse. Een venster dat een veelvoud van 7 dagen is houdt het weekpatroon zuiver. |
| Model | *statsmodels* (standaard): snel en stabiel. *tfcausalimpact*: het originele Google-model; zie [Hoe het werkt](#hoe-het-werkt). |
| α | Significantieniveau, standaard 0,05. |
| Placebotest | Draait hetzelfde model op een nepdatum in de pre-periode. Standaard aan. |

Een post-periode van minimaal 3 tot 6 weken is in SEO gebruikelijk. De pre-periode moet minimaal 14 meetpunten hebben.

---

## Het resultaat lezen

Bovenaan staat een conclusie in gewone taal. Daaronder:

| Onderdeel | Betekenis |
|---|---|
| Relatief effect | Hoeveel procent de testgroep boven of onder de verwachting zat, met de bandbreedte (95%-interval). Omvat de bandbreedte nul, dan is er geen aantoonbaar effect. |
| Cumulatief | Het totaal aantal extra (of minder) klikken over de hele meetperiode. |
| p-waarde | De kans op dit resultaat zonder echt effect. Onder α heet het significant. |
| Pre-periode fit | Hoe goed de controle de testgroep vóór de test voorspelde (correlatie en gemiddelde afwijking). Onder r = 0,9 is de voorspelling matig. |

De grafiek heeft drie panelen:

1. **Werkelijk tegen verwacht**: de testgroep tegen wat zonder ingreep verwacht werd, met de bandbreedte.
2. **Effect per periode**: het verschil per dag of week.
3. **Cumulatief effect**: het opgetelde verschil vanaf de meetstart. Loopt die lijn gestaag op, dan is het effect blijvend. Piekt hij en zakt hij terug, dan was het een tijdelijke prikkel of ruis.

Het grijze vlak is het uitrolvenster dat buiten de analyse blijft.

**Placebotest:** vindt het model op een nepdatum een significant effect, dan liepen test en controle al uit elkaar vóór de ingreep, en is het resultaat onbetrouwbaar. De app zegt dat dan in de conclusie.

Downloads: de grafiek (PNG), alle cijfers (JSON) en de reeksen achter de grafiek (CSV).

---

## Geplakte en geüploade data

De app herkent zelf het formaat:

- **CSV** met komma's, **TSV** (zoals kopiëren uit BigQuery of Sheets), **puntkomma-CSV** met Nederlandse getallen (`1.234,5`), **JSON** of **NDJSON**.
- Kolomnamen mogen in het Nederlands of Engels: `date`/`data_date`/`datum`, `url`/`page`/`pagina`, `clicks`/`klikken`, `impressions`/`vertoningen`, `groep`/`group`, `position`/`positie` of `sum_position`.

Drie vormen worden herkend:

| Vorm | Kolommen | Gebruikt in |
|---|---|---|
| Url-niveau | `url, date, clicks, impressions, sum_position` | Stap 1 en 2 |
| Groepsniveau | `date, groep, clicks, impressions` | Stap 2 |
| Breed | `date` plus een kolom per reeks | Stap 2, bron *Eigen CSV* |

`sum_position` volgt de bulkexport: gemiddelde positie = `sum_position / impressions + 1`. Heb je alleen een gemiddelde positie, gebruik dan de kolom `position`.

---

## Problemen oplossen

| Melding of probleem | Oplossing |
|---|---|
| `externally-managed-environment` | Je installeert in de Homebrew-Python. Gebruik de venv: `python3 -m venv .venv` en daarna `.venv/bin/python -m pip install -r requirements.txt`. |
| `ModuleNotFoundError: No module named ...` | Streamlit draait met een andere Python dan waar de pakketten staan. Start met `.venv/bin/python -m streamlit run app.py`. |
| App stopt niet | Ctrl + C in de terminal waarin hij draait, of `pkill -f streamlit`. |
| *Geen gcloud-login gevonden* | Draai `gcloud auth application-default login` met de scopes uit [Inloggen](#inloggen). Controleer met `ls ~/.config/gcloud/application_default_credentials.json`. |
| `GOOGLE_APPLICATION_CREDENTIALS` verwijst naar een bestand | Bij gcloud-login slaat de app een verwijzing naar een niet-bestaand sleutelbestand zelf over. Wil je hem ook voor andere tools kwijt: `grep -n GOOGLE_APPLICATION_CREDENTIALS ~/.zshrc ~/.zprofile`, regel weghalen, nieuwe terminal. |
| *Je gcloud-account mag ... niet gebruiken* | Je mist de rol *Service Account Token Creator* op dat serviceaccount. De app toont het commando; `scripts/nieuwe-klant.sh` regelt het ook. |
| *Een API staat uit in je quotaproject* | `gcloud services enable searchconsole.googleapis.com bigquery.googleapis.com --project JOUW_QUOTAPROJECT` |
| Property-lijst is leeg | Het account is niet toegevoegd in Search Console voor die property. |
| BigQuery-export-lijst is leeg | Het account heeft geen leesrecht op de exportdataset, of de BigQuery API staat uit in het quotaproject. Je kunt de tabel ook gewoon intypen. |
| *Te weinig url's over* | Verruim de patronen of verlaag *Min. klikken per url*. |
| Toetsen falen steeds | Lage correlatie: te weinig verkeer per groep, meet per week of verruim de scope. Parallelle trend faalt: kies een andere pre-periode zonder grote gebeurtenis erin. SMD faalt: meer verkeersklassen of een andere seed. |
| *Minder dan 80% van de url's gevonden* | Property, land of patronen in stap 2 wijken af van stap 1. Upload `testconfig.json`. |
| tfcausalimpact loopt vast | Bekend probleem met sommige TensorFlow-versies. De app draait hem in een apart proces met een limiet van 300 seconden en valt dan terug op statsmodels. |

---

## Online draaien (share.streamlit.io)

Kan, maar zonder gcloud-login: die bestaat daar niet.

- De bron **BigQuery (query + plakken)** werkt gewoon.
- Voor de **GSC API** upload je per sessie een JSON-key in de zijbalk. Die wordt niet opgeslagen.
- Kies bij het deployen Python 3.12. TensorFlow staat bewust niet in `requirements.txt`, dus online draait het statsmodels-model.
- De data van je klanten loopt dan via de servers van Streamlit. Check of dat past bij je afspraken.
- Een app uit een publieke repo is standaard openbaar. Je kunt hem in de instellingen beperken tot uitgenodigde mensen; op Community Cloud mag je één privé-app tegelijk hebben.

---

## Hoe het werkt

### Groepsindeling

1. Per url berekent de app uit de pre-periode: klikken, vertoningen, CTR, gemiddelde positie en trend (klikken in de tweede helft tegen de eerste helft).
2. Url's onder de klikdrempel en uitschieters gaan eruit.
3. Strata: paginatype × verkeersklasse × trendklasse (dalend, stabiel, stijgend). Een te klein stratum valt terug op paginatype × verkeersklasse, daarna op paginatype, en als laatste op `rest`.
4. Binnen elk stratum sorteert de app de url's op klikken, deelt ze op in blokjes van *k* url's (*k* is het aantal groepen) en loot binnen elk blokje welke url in welke groep komt. Dat geeft gematchte groepen in plaats van puur toeval. Wat overblijft gaat naar de kleinste groep.
5. De toetsen bepalen of de indeling goed genoeg is; zo niet, dan volgt een nieuwe seed.

### Effectmeting

- **statsmodels** (standaard): een structureel tijdreeksmodel met een lokaal niveau en de controle als regressor, gefit op de pre-periode. De niveauvariantie staat vast op een kleine waarde, net als in CausalImpact; zonder die rem neemt het niveau het effect over. Er is geen eigen weekseizoen, omdat de controle dat patroon al meeneemt. De verwachting na de livegang komt uit 1.000 gesimuleerde paden, waaruit de bandbreedte en de p-waarde volgen. Onzekerheid in de modelparameters telt niet mee, dus de bandbreedte is iets smaller dan bij tfcausalimpact.
- **tfcausalimpact**: het CausalImpact-model van Google (TensorFlow Probability). Zwaar, en bij sommige TensorFlow-versies loopt het vast. Het draait daarom in een apart proces met een tijdslimiet. Installeren: `.venv/bin/python -m pip install -r requirements-tf.txt` (Python 3.9 tot en met 3.12).

Beide geven dezelfde uitkomsten en grafiek. Op de demodata met een ingebouwd effect van +8% vonden ze +8,0% (statsmodels) en +7,5% (tfcausalimpact); zonder ingebouwd effect +0,1%, niet significant.

### Queries

Alle queries zijn losse `SELECT`'s op `searchdata_url_impression`, met filters op datum, zoektype, land, apparaat en url-patronen. De query van stap 2 bevat de indeling als `UNNEST`-lijsten; het gemeenschappelijke begin van de url's (schema en host) wordt eraf gehaald om de query klein te houden.

---

## Ontwikkelen en testen

```bash
.venv/bin/python -m pytest -q tests
```

De tests controleren onder meer de balans van de indeling, dat een ingebouwd effect van +8% wordt teruggevonden en een situatie zonder effect niet significant is, het lezen van geplakte tabellen, de queries, het inloggen en de complete app-flows.

```
app.py                     Streamlit-app (zijbalk, stap 1 en 2)
core/grouping.py           stratificatie, toewijzing, balanstoetsen, MDE
core/impact.py             CausalImpact (statsmodels en tfcausalimpact), placebo, grafiek
core/_tfci_worker.py       tfcausalimpact in een apart proces
core/sql_builder.py        BigQuery-queries voor stap 1 en 2
core/gsc_api.py            inloggen (gcloud of JSON-key) en de Search Console API
core/bq_discovery.py       GSC-exports vinden in BigQuery
core/data_io.py            plakken en uploaden, normaliseren, NL-getallen, demodata
scripts/nieuwe-klant.sh    serviceaccount per klant, zonder sleutel
serviceaccounts.example.txt voorbeeld voor serviceaccounts.txt
tests/test_flow.py         tests
```

Licentie: MIT.
