"""Search Console API access: gcloud login (with service-account impersonation)
or a service-account JSON key.

The account the app acts as needs (restricted) access to the property in
Search Console. Data comes back as url × day in the internal long format.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Callable

import pandas as pd

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/cloud-platform",  # BigQuery: find GSC exports
]
ROW_LIMIT = 25_000
DAILY_ROW_CAP = 50_000  # API maximum per day per search type


def credentials_from_json(raw: str | bytes | dict):
    from google.oauth2 import service_account

    info = raw if isinstance(raw, dict) else json.loads(raw)
    if info.get("type") != "service_account":
        raise ValueError("Dit is geen serviceaccount-sleutel (type ≠ service_account).")
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def credentials_from_file(path: str):
    with open(path, "rb") as fh:
        return credentials_from_json(fh.read())


class LoginError(RuntimeError):
    """Login failed with a fix the user can act on."""


def credentials_from_gcloud(service_account: str | None = None):
    """Use the gcloud login on this machine (Application Default Credentials).

    With `service_account`, the app acts as that service account through
    impersonation: no key file, a short-lived token (1 hour). This needs the
    role Service Account Token Creator for the logged-in user on that account.
    Without it, the app acts as the logged-in user; then the login must include
    the Search Console scope.
    """
    import os

    import google.auth
    from google.auth import impersonated_credentials
    from google.auth.exceptions import DefaultCredentialsError

    # A stale GOOGLE_APPLICATION_CREDENTIALS (pointing to a missing key file) makes
    # Google ignore the gcloud login. For gcloud-login we skip it in that case.
    stale = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if stale and not os.path.isfile(os.path.expanduser(stale)):
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS")

    try:
        source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except DefaultCredentialsError as exc:
        import os

        env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        hint = (
            f"GOOGLE_APPLICATION_CREDENTIALS verwijst naar {env}, en Google zoekt daar eerst. "
            "Haal die weg met: unset GOOGLE_APPLICATION_CREDENTIALS (en start de app opnieuw)."
            if env else
            "Geen gcloud-login gevonden op deze computer. Draai eerst:\n"
            "gcloud auth application-default login"
        )
        raise LoginError(f"{hint}\n\nOorzaak volgens Google: {exc}") from exc

    if not service_account:
        user, _ = google.auth.default(scopes=SCOPES)
        user.service_account_email = getattr(user, "account", None) or "eigen gcloud-account"
        return user

    creds = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=service_account.strip(),
        target_scopes=SCOPES,
        lifetime=3600,
    )
    return creds  # exposes .service_account_email (the target principal)


def explain_error(exc: Exception, service_account: str | None = None) -> str:
    """Turn common Google auth errors into a Dutch fix."""
    msg = str(exc)
    if isinstance(exc, LoginError):
        return msg
    if service_account and ("iam.serviceAccounts.getAccessToken" in msg or "PERMISSION_DENIED" in msg):
        return (
            f"Je gcloud-account mag {service_account} niet gebruiken. Geef jezelf eenmalig die rol:\n"
            f"gcloud iam service-accounts add-iam-policy-binding {service_account} "
            "--member=\"user:JOUW_EMAIL\" --role=\"roles/iam.serviceAccountTokenCreator\""
        )
    if "insufficient" in msg.lower() and "scope" in msg.lower():
        return (
            "Je gcloud-login mist het Search Console-recht. Kies een serviceaccount, of log opnieuw in met:\n"
            "gcloud auth application-default login --scopes="
            "https://www.googleapis.com/auth/webmasters.readonly,https://www.googleapis.com/auth/cloud-platform"
        )
    if "has not been used" in msg or "SERVICE_DISABLED" in msg:
        api = "bigquery.googleapis.com" if "bigquery" in msg.lower() else "searchconsole.googleapis.com"
        return ("Een API staat uit in je quotaproject. Zet hem aan met:\n"
                f"gcloud services enable {api} --project JOUW_QUOTAPROJECT\n\n{msg}")
    return msg


def _service(creds):
    from googleapiclient.discovery import build

    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def list_sites(creds) -> list[str]:
    resp = _service(creds).sites().list().execute()
    return sorted(s["siteUrl"] for s in resp.get("siteEntry", []) if s.get("permissionLevel") != "siteUnverifiedUser")


def fetch_url_day(
    creds,
    site_url: str,
    start: date,
    end: date,
    countries: list[str] | None = None,
    devices: list[str] | None = None,
    search_type: str = "web",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    data_state: str = "final",
    progress: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Fetch clicks/impressions/position per page per day.

    The API cannot OR countries or devices inside one filter group, so we run one
    request series per country × device and sum the result. Requests run in
    monthly chunks to stay well under the per-request row cap.
    """
    svc = _service(creds)
    countries = countries or [None]
    devices = devices or [None]
    include = "|".join(f"(?:{p})" for p in include_patterns or [])
    frames = []

    chunks = []
    cur = start
    while cur <= end:
        nxt = min(end, (pd.Timestamp(cur) + pd.offsets.MonthEnd(0)).date())
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)

    for country in countries:
        for device in devices:
            filters = []
            if include:
                filters.append({"dimension": "page", "operator": "includingRegex", "expression": include})
            for p in exclude_patterns or []:
                filters.append({"dimension": "page", "operator": "excludingRegex", "expression": p})
            if country:
                filters.append({"dimension": "country", "operator": "equals", "expression": country.lower()})
            if device:
                filters.append({"dimension": "device", "operator": "equals", "expression": device.upper()})

            for c_start, c_end in chunks:
                start_row = 0
                while True:
                    body = {
                        "startDate": str(c_start),
                        "endDate": str(c_end),
                        "dimensions": ["date", "page"],
                        "type": search_type.lower(),
                        "aggregationType": "byPage",
                        "dataState": data_state,
                        "rowLimit": ROW_LIMIT,
                        "startRow": start_row,
                    }
                    if filters:
                        body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
                    resp = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
                    rows = resp.get("rows", [])
                    if rows:
                        df = pd.DataFrame({
                            "date": [r["keys"][0] for r in rows],
                            "url": [r["keys"][1] for r in rows],
                            "clicks": [r.get("clicks", 0) for r in rows],
                            "impressions": [r.get("impressions", 0) for r in rows],
                            "position": [r.get("position", 0) for r in rows],
                        })
                        df["sum_position"] = (df["position"] - 1).clip(lower=0) * df["impressions"]
                        frames.append(df.drop(columns="position"))
                    if progress:
                        label = " · ".join(x for x in [country, device] if x) or "alles"
                        progress(f"{label}: {c_start} t/m {c_end}, {start_row + len(rows):,} rijen".replace(",", "."))
                    if len(rows) < ROW_LIMIT:
                        break
                    start_row += ROW_LIMIT

    if not frames:
        return pd.DataFrame(columns=["url", "date", "clicks", "impressions", "sum_position"])
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    # The API returns at most ~50.000 rows per day per search type. A day close
    # to that cap probably misses low-traffic URLs (they then look like zeros).
    per_day = out.groupby("date").size()
    capped = per_day[per_day >= DAILY_ROW_CAP * 0.98]
    out = out.groupby(["url", "date"], as_index=False)[["clicks", "impressions", "sum_position"]].sum()
    out.attrs["capped_days"] = [d.date().isoformat() for d in capped.index]
    return out
