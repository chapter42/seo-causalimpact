"""Search Console API access with a service account.

The service account needs (restricted) access to the property in Search
Console. Data comes back as url × day in the internal long format.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Callable

import pandas as pd

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
ROW_LIMIT = 25_000


def credentials_from_json(raw: str | bytes | dict):
    from google.oauth2 import service_account

    info = raw if isinstance(raw, dict) else json.loads(raw)
    if info.get("type") != "service_account":
        raise ValueError("Dit is geen serviceaccount-sleutel (type ≠ service_account).")
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def credentials_from_file(path: str):
    with open(path, "rb") as fh:
        return credentials_from_json(fh.read())


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
    return out.groupby(["url", "date"], as_index=False)[["clicks", "impressions", "sum_position"]].sum()
