"""Data loading, normalisation and formatting helpers.

Internal "long" format used everywhere:
    url (str) | date (Timestamp, period start) | clicks | impressions | sum_position

`sum_position` follows the GSC bulk export convention (zero-based, summed over
impressions), so average position = sum_position / impressions + 1.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Iterable

import numpy as np
import pandas as pd

LONG_COLS = ["url", "date", "clicks", "impressions", "sum_position"]

_ALIASES = {
    "date": ["date", "data_date", "datum", "day", "dag", "period", "periode", "week", "week_start"],
    "url": ["url", "page", "pagina", "landing_page", "landingpage"],
    "clicks": ["clicks", "klikken", "kliks"],
    "impressions": ["impressions", "vertoningen", "impressies"],
    "sum_position": ["sum_position"],
    "position": ["position", "avg_position", "positie", "gem_positie"],
    "groep": ["groep", "group", "test_group", "bucket", "variant"],
    "n_urls": ["n_urls", "urls"],
}


# --- Number formatting (nl-NL: 1.000,00) -------------------------------------------
def nl(x: float | int | None, dec: int = 0, sign: bool = False) -> str:
    """Format a number Dutch style: 1.234,56."""
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    s = f"{x:+,.{dec}f}" if sign else f"{x:,.{dec}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def nl_pct(x: float | None, dec: int = 1, sign: bool = True) -> str:
    """Format a fraction (0.123) as a Dutch percentage (+12,3%)."""
    if x is None or not np.isfinite(x):
        return "–"
    return nl(x * 100, dec, sign) + "%"


# --- Parsing pasted / uploaded tables -------------------------------------------
def parse_table(raw: str | bytes) -> pd.DataFrame:
    """Parse a pasted or uploaded table: CSV, TSV, semicolon-CSV, JSON or NDJSON."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    text = text.strip().lstrip("﻿")
    if not text:
        raise ValueError("Geen data gevonden.")

    if text[0] in "[{":
        try:
            return pd.read_json(io.StringIO(text), dtype=False)
        except ValueError:
            return pd.read_json(io.StringIO(text), lines=True, dtype=False)

    sample = "\n".join(text.splitlines()[:20])
    try:
        sep = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        first = text.splitlines()[0]
        sep = max([",", "\t", ";", "|"], key=first.count)
    # Read as text: number and date formats are decided per column afterwards
    # (to_number / to_dates), so "1.234" or "01-09-2026" are not misread here.
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)


_THOUSANDS_DOT = re.compile(r"^-?\d{1,3}(\.\d{3})+$")
_THOUSANDS_COMMA = re.compile(r"^-?\d{1,3}(,\d{3})+$")


def _to_number(s: pd.Series, integer: bool = False) -> pd.Series:
    """Numeric conversion that understands Dutch and US notation.

    Decided per column, from all values together:
    - both '.' and ',' present: the one that comes last in a value is the decimal sign;
    - only ',': thousands if every value looks like 1,234,567; otherwise decimal comma;
    - only '.': thousands if `integer` (clicks, impressions) and every value with a
      dot looks like 1.234 or 1.234.567; otherwise decimal point.
    """
    if pd.api.types.is_numeric_dtype(s):
        return s
    t = s.astype(str).str.strip().str.replace("\u00a0", "", regex=False).str.replace(" ", "", regex=False)
    t = t.replace({"": None, "nan": None, "None": None, "null": None, "NaN": None})
    vals = t.dropna()
    has_dot = vals.str.contains(".", regex=False)
    has_comma = vals.str.contains(",", regex=False)
    if (has_dot & has_comma).any():
        both = vals[has_dot & has_comma]
        comma_last = (both.str.rfind(",") > both.str.rfind(".")).mean() >= 0.5
        t = t.str.replace(".", "", regex=False).str.replace(",", ".", regex=False) if comma_last \
            else t.str.replace(",", "", regex=False)
    elif has_comma.any():
        if vals[has_comma].str.match(_THOUSANDS_COMMA).all() and integer:
            t = t.str.replace(",", "", regex=False)
        else:
            t = t.str.replace(",", ".", regex=False)
    elif has_dot.any() and integer and vals[has_dot].str.match(_THOUSANDS_DOT).all():
        t = t.str.replace(".", "", regex=False)
    return pd.to_numeric(t, errors="coerce")


def to_number(s: pd.Series, name: str, integer: bool = False) -> pd.Series:
    """_to_number plus a clear error when values cannot be read (instead of a silent 0)."""
    raw = s.astype(str).str.strip()
    out = _to_number(s, integer=integer)
    bad = out.isna() & ~raw.isin(["", "nan", "None", "null", "NaN"])
    if bad.any():
        ex = ", ".join(raw[bad].unique()[:3])
        raise ValueError(f"Kolom '{name}': {bad.sum()} waarde(n) zijn geen getal (bijv. {ex}).")
    return out


def to_dates(s: pd.Series) -> pd.Series:
    """Dates in ISO (2026-09-01), Dutch (01-09-2026, 1-9-2026) or with a time.

    ISO is tried first; otherwise day-first. Fails loudly when values remain
    unreadable, instead of silently dropping or swapping day and month.
    """
    raw = s.astype(str).str.strip()
    iso = pd.to_datetime(raw, format="ISO8601", errors="coerce")
    if iso.notna().all():
        out = iso
    else:
        out = pd.to_datetime(raw, dayfirst=True, errors="coerce", format="mixed")
    out = out.dt.tz_localize(None) if getattr(out.dt, "tz", None) is not None else out
    bad = out.isna() & ~raw.isin(["", "nan", "None", "NaT"])
    if bad.any():
        ex = ", ".join(raw[bad].unique()[:3])
        raise ValueError(f"{bad.sum()} datum(s) zijn niet te lezen (bijv. {ex}). Gebruik bij voorkeur 2026-09-01.")
    return out.dt.normalize()


def normalize_url(u: pd.Series) -> pd.Series:
    """Matching key for URLs: no fragment, lowercase scheme and host, no spaces.
    The path stays as is (a trailing slash can be a different page)."""
    u = u.astype(str).str.strip().str.split("#").str[0]
    m = u.str.extract(r"^(?P<head>[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]*)(?P<rest>.*)$")
    return m["head"].str.lower().fillna("") + m["rest"].where(m["head"].notna(), u)


def standardise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known column aliases to canonical names."""
    rename = {}
    lower = {c: re.sub(r"\s+", "_", str(c).strip().lower()) for c in df.columns}
    for canon, aliases in _ALIASES.items():
        for col, low in lower.items():
            if low in aliases and canon not in rename.values():
                rename[col] = canon
                break
    return df.rename(columns=rename)


def to_long(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a URL-level table (url × date) to the internal long format."""
    df = standardise_columns(df)
    missing = {"url", "date", "clicks"} - set(df.columns)
    if missing:
        raise ValueError(
            f"Kolommen ontbreken: {', '.join(sorted(missing))}. "
            f"Gevonden: {', '.join(map(str, df.columns))}"
        )
    out = pd.DataFrame({"url": df["url"].astype(str).str.strip()})
    out["date"] = to_dates(df["date"])
    out["clicks"] = to_number(df["clicks"], "clicks", integer=True).fillna(0)
    out["impressions"] = to_number(df["impressions"], "impressions", integer=True).fillna(0) if "impressions" in df else np.nan
    if "sum_position" in df:
        out["sum_position"] = to_number(df["sum_position"], "sum_position").fillna(0)
    elif "position" in df and "impressions" in df:
        out["sum_position"] = (to_number(df["position"], "position") - 1).clip(lower=0) * out["impressions"]
    else:
        out["sum_position"] = np.nan
    out = out.dropna(subset=["date"])
    out = (
        out.groupby(["url", "date"], as_index=False)[["clicks", "impressions", "sum_position"]]
        .sum(min_count=1)
    )
    return out[LONG_COLS]


def to_group_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a group-level table (date × groep) from the analysis query."""
    df = standardise_columns(df)
    missing = {"date", "groep", "clicks"} - set(df.columns)
    if missing:
        raise ValueError(f"Kolommen ontbreken: {', '.join(sorted(missing))}.")
    out = pd.DataFrame({
        "date": to_dates(df["date"]),
        "groep": df["groep"].astype(str).str.strip(),
        "clicks": to_number(df["clicks"], "clicks", integer=True).fillna(0),
        "impressions": to_number(df["impressions"], "impressions", integer=True).fillna(0) if "impressions" in df else np.nan,
    })
    return out.dropna(subset=["date"]).groupby(["date", "groep"], as_index=False).sum()


def detect_shape(df: pd.DataFrame) -> str:
    """Return 'long' (url × date), 'group' (date × groep) or 'wide' (date + series)."""
    cols = set(standardise_columns(df).columns)
    if {"url", "date", "clicks"} <= cols:
        return "long"
    if {"groep", "date", "clicks"} <= cols:
        return "group"
    return "wide"


# --- Aggregation ----------------------------------------------------------------
def snap_to_monday(dates: pd.Series | pd.Index) -> pd.Series:
    d = pd.to_datetime(pd.Series(dates))
    return d - pd.to_timedelta(d.dt.weekday, unit="D")


def to_period(long: pd.DataFrame, granularity: str) -> pd.DataFrame:
    """Aggregate daily long data to weeks (Monday start) when asked.

    Incomplete weeks at the start or end (fewer than 7 days in the data) are
    dropped: a half week looks like a drop in traffic and biases the model.
    """
    if granularity != "week":
        return long
    out = long.copy()
    days = pd.Series(out["date"].unique())
    weeks = snap_to_monday(days)
    full = weeks.groupby(weeks.values).size()
    complete = set(full[full >= 7].index)
    out["date"] = snap_to_monday(out["date"]).to_numpy()
    out = out[out["date"].isin(complete)]
    return out.groupby(["url", "date"], as_index=False)[["clicks", "impressions", "sum_position"]].sum(min_count=1)


def infer_granularity(dates: Iterable) -> str:
    d = pd.Series(sorted(pd.to_datetime(pd.Series(list(dates))).unique()))
    if len(d) < 3:
        return "day"
    step = d.diff().dt.days.median()
    return "week" if step >= 6 else "day"


def _with_key(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(_key=normalize_url(df["url"]))


def group_series(
    long: pd.DataFrame, assignments: pd.DataFrame, metric: str = "clicks", groups: list[str] | None = None
) -> pd.DataFrame:
    """Sum a URL-level metric per group per period → wide frame (date × groep).

    URLs are matched on a normalised key. Weekly data is snapped to Mondays, so
    weeks that start on another day are not lost. Columns follow `groups`
    (control first) when given.
    """
    m = _with_key(long).merge(_with_key(assignments)[["_key", "groep"]], on="_key", how="inner")
    weekly = infer_granularity(m["date"]) == "week"
    if weekly:
        m = m.assign(date=snap_to_monday(m["date"]).to_numpy())
    wide = m.pivot_table(index="date", columns="groep", values=metric, aggfunc="sum").sort_index()
    full = pd.date_range(wide.index.min(), wide.index.max(), freq="W-MON" if weekly else "D")
    wide = wide.reindex(full).fillna(0)
    if groups:
        wide = wide[[g for g in groups if g in wide.columns] + [c for c in wide.columns if c not in groups]]
    return wide


def match_rate(long: pd.DataFrame, assignments: pd.DataFrame) -> tuple[float, list[str]]:
    """Share of assignment URLs found in the data, plus the URLs not found."""
    keys = _with_key(assignments)
    found = set(normalize_url(long["url"]).unique())
    missing = keys.loc[~keys["_key"].isin(found), "url"].tolist()
    return 1 - len(missing) / max(len(keys), 1), missing


def order_groups(groups: list[str], cfg: dict | None) -> list[str]:
    """Control first. Uses the group order saved in testconfig.json; otherwise a
    group whose name contains 'contr' goes first; otherwise the given order."""
    names = ((cfg or {}).get("indeling") or {}).get("group_names") or []
    ordered = [g for g in names if g in groups]
    if ordered:
        return ordered + [g for g in groups if g not in ordered]
    ctrl = [g for g in groups if "contr" in g.lower()]
    return ctrl + [g for g in groups if g not in ctrl]


# --- Demo data ------------------------------------------------------------------
def make_demo_long(
    n_urls: int = 600,
    start: str = "2026-05-04",
    days: int = 182,
    seed: int = 7,
) -> pd.DataFrame:
    """Synthetic GSC-like url × day data with three page types, skewed traffic,
    shared weekly seasonality and a few URLs with their own trend."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=days, freq="D")
    t = np.arange(days)
    types = rng.choice(["p", "l", "inf"], size=n_urls, p=[0.6, 0.3, 0.1])
    base = np.exp(rng.normal(2.2, 1.1, n_urls))  # long-tail traffic
    own_trend = rng.normal(0, 0.0015, n_urls)
    season = 1 + 0.18 * np.sin(2 * np.pi * t / 7) + 0.10 * np.sin(2 * np.pi * t / 91)
    rows = []
    for i in range(n_urls):
        lam = base[i] * season * np.exp(own_trend[i] * t)
        clicks = rng.poisson(lam)
        impr = rng.poisson(lam * rng.uniform(12, 30))
        pos = np.clip(rng.normal(rng.uniform(2, 15), 1.0, days), 1, None)
        rows.append(pd.DataFrame({
            "url": f"https://www.voorbeeld.nl/{types[i]}/item-{i:04d}/",
            "date": dates,
            "clicks": clicks,
            "impressions": impr,
            "sum_position": (pos - 1) * impr,
        }))
    return pd.concat(rows, ignore_index=True)


def apply_demo_effect(
    long: pd.DataFrame, assignments: pd.DataFrame, group: str, start: str, uplift: float
) -> pd.DataFrame:
    """Inject a multiplicative uplift for one group from `start` on (demo only)."""
    out = long.copy()
    urls = set(assignments.loc[assignments["groep"] == group, "url"])
    mask = out["url"].isin(urls) & (out["date"] >= pd.Timestamp(start))
    rng = np.random.default_rng(1)
    out.loc[mask, "clicks"] = rng.poisson(out.loc[mask, "clicks"] * (1 + uplift))
    return out


# --- Test config (reproducibility) ----------------------------------------------
def dump_config(cfg: dict) -> str:
    return json.dumps(cfg, indent=2, ensure_ascii=False, default=str)


def load_config(raw: str | bytes) -> dict:
    return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
