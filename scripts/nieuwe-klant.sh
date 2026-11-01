#!/usr/bin/env bash
# Maakt voor één klant een serviceaccount voor de SEO-test-app, zonder sleutelbestand.
# Jij (je gcloud-account) mag daarna als dat serviceaccount handelen.
#
# Gebruik:  bash scripts/nieuwe-klant.sh <gcp-project-id> <klantnaam>
# Voorbeeld: bash scripts/nieuwe-klant.sh chapter42-klantx klantx
#
# Vooraf, eenmalig:  gcloud auth login  én  gcloud auth application-default login
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Gebruik: bash scripts/nieuwe-klant.sh <gcp-project-id> <klantnaam>" >&2
  exit 1
fi
PROJECT="$1"
KLANT="$2"
ME="$(gcloud config get-value account 2>/dev/null)"
if [ -z "$ME" ]; then
  echo "Geen gcloud-account actief. Draai eerst: gcloud auth login" >&2
  exit 1
fi

# Naam van het serviceaccount: seo-<klant>. Google eist 6 tot 30 tekens:
# kleine letters, cijfers en streepjes, beginnend met een letter.
SLUG="$(echo "$KLANT" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+|-+$//g')"
if [ -z "$SLUG" ]; then
  echo "Klantnaam '${KLANT}' bevat geen bruikbare tekens (a-z, 0-9). Kies een andere naam." >&2
  exit 1
fi
SA_NAME="seo-${SLUG}"
[ ${#SA_NAME} -lt 6 ] && SA_NAME="seo-klant-${SLUG}"
SA_NAME="$(echo "$SA_NAME" | cut -c1-30 | sed -E 's/-+$//')"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "1/4 Project ${PROJECT}"
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  gcloud projects create "$PROJECT"
fi

echo "2/4 API's aanzetten (Search Console, IAM Credentials, BigQuery)"
gcloud services enable searchconsole.googleapis.com iamcredentials.googleapis.com bigquery.googleapis.com \
  --project "$PROJECT"

echo "3/4 Serviceaccount ${SA}"
if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "SEO-test ${KLANT}"
fi

echo "4/4 ${ME} mag als dit serviceaccount handelen"
# Een net aangemaakt account is niet meteen overal bekend: een paar keer proberen.
for poging in 1 2 3 4 5 6; do
  if gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" \
       --member "user:${ME}" --role "roles/iam.serviceAccountTokenCreator" --quiet >/dev/null 2>&1; then
    break
  fi
  if [ "$poging" -eq 6 ]; then
    echo "Rol toekennen lukte niet. Probeer over een minuut opnieuw:" >&2
    echo "  gcloud iam service-accounts add-iam-policy-binding $SA --project $PROJECT --member user:${ME} --role roles/iam.serviceAccountTokenCreator" >&2
    exit 1
  fi
  echo "   account nog niet actief, opnieuw over 10 seconden (poging ${poging} van 6)"
  sleep 10
done

# Onthouden voor de app (lokaal bestand, staat in .gitignore).
LIST="$(cd "$(dirname "$0")/.." && pwd)/serviceaccounts.txt"
grep -qF "= ${SA}" "$LIST" 2>/dev/null || echo "${KLANT} = ${SA}" >> "$LIST"

cat <<EOF

Klaar. Nog één ding met de hand:
  Search Console → property van ${KLANT} → Instellingen → Gebruikers en rechten
  → Gebruiker toevoegen: ${SA}  (recht: Beperkt)

Voor BigQuery (optioneel), in het project van de klant:
  gcloud projects add-iam-policy-binding <klantproject> --member "serviceAccount:${SA}" --role roles/bigquery.jobUser
  en op de dataset met de GSC-export de rol BigQuery Data Viewer.

In de app: 🔑 Inloggen → gcloud-login → Handelen als "${KLANT}" → Verbinden.
EOF
