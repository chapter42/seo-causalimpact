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
            return pd.read_json(io.StringIO(text))
        except ValueError:
            return pd.read_json(io.StringIO(text), lines=True)

    sample = "\n".join(text.splitlines()[:20])
    try:
        sep = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        first = text.splitlines()[0]
        sep = max([",", "\t", ";", "|"], key=first.count)
    if sep == ";":  # Dutch Excel/Sheets export: 1.234,5
        return pd.read_csv(io.StringIO(text), sep=sep, decimal=",", thousands=".")
    return pd.read_csv(io.StringIO(text), sep=sep)


def _to_number(s: pd.Series) -> pd.Series:
    """Numeric conversion that also accepts Dutch notation (1.234,5)."""
    if pd.api.types.is_numeric_dtype(s):
        return s
    st = s.astype(str).str.strip().str.replace(" ", "", regex=False)
    if st.str.contains(",", regex=False).any():
        st = st.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    return pd.to_numeric(st, errors="coerce")


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
    out["date"] = pd.to_datetime(df["date"], errors="coerce")
    out["clicks"] = _to_number(df["clicks"]).fillna(0)
    out["impressions"] = _to_number(df["impressions"]).fillna(0) if "impressions" in df else np.nan
    if "sum_position" in df:
        out["sum_position"] = _to_number(df["sum_position"]).fillna(0)
    elif "position" in df and "impressions" in df:
        out["sum_position"] = (_to_number(df["position"]) - 1).clip(lower=0) * out["impressions"]
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
        "date": pd.to_datetime(df["date"], errors="coerce"),
        "groep": df["groep"].astype(str),
        "clicks": _to_number(df["clicks"]).fillna(0),
        "impressions": _to_number(df["impressions"]).fillna(0) if "impressions" in df else np.nan,
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
def to_period(long: pd.DataFrame, granularity: str) -> pd.DataFrame:
    """Aggregate daily long data to weeks (Monday start) when asked."""
    if granularity != "week":
        return long
    out = long.copy()
    out["date"] = out["date"] - pd.to_timedelta(out["date"].dt.weekday, unit="D")
    return out.groupby(["url", "date"], as_index=False)[["clicks", "impressions", "sum_position"]].sum(min_count=1)


def infer_granularity(dates: Iterable) -> str:
    d = pd.Series(sorted(pd.to_datetime(pd.Series(list(dates))).unique()))
    if len(d) < 3:
        return "day"
    step = d.diff().dt.days.median()
    return "week" if step >= 6 else "day"


def group_series(
    long: pd.DataFrame, assignments: pd.DataFrame, metric: str = "clicks"
) -> pd.DataFrame:
    """Sum a URL-level metric per group per period → wide frame (date × groep)."""
    m = long.merge(assignments[["url", "groep"]], on="url", how="inner")
    wide = m.pivot_table(index="date", columns="groep", values=metric, aggfunc="sum").sort_index()
    full = pd.date_range(wide.index.min(), wide.index.max(), freq="W-MON" if infer_granularity(wide.index) == "week" else "D")
    return wide.reindex(full).fillna(0)


def match_rate(long: pd.DataFrame, assignments: pd.DataFrame) -> float:
    urls = set(assignments["url"])
    found = urls & set(long["url"].unique())
    return len(found) / max(len(urls), 1)


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
