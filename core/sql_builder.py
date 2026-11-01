"""Generates BigQuery SQL for the GSC bulk export (searchdata_url_impression).

The user runs the query in the BigQuery console and pastes or uploads the
result. All queries are plain SELECTs: no tables are created.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .data_io import nl

MAX_QUERY_BYTES = 1_000_000  # BigQuery limit for the query text (unresolved)


@dataclass
class SourceFilters:
    table: str = "project.searchconsole.searchdata_url_impression"
    start: date | None = None
    end: date | None = None
    countries: list[str] = field(default_factory=lambda: ["nld"])
    devices: list[str] = field(default_factory=list)
    search_type: str = "WEB"
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    granularity: str = "day"  # day | week
    min_impressions: int = 0

    def full_table(self) -> str:
        t = self.table.strip().strip("`")
        return t if t.count(".") >= 2 else f"{t}.searchdata_url_impression"


def _raw(p: str) -> str:
    """Regex as a BigQuery raw string: r'...', or with double quotes when the
    pattern itself contains a single quote."""
    if p.endswith("\\") and not p.endswith("\\\\"):
        raise ValueError(f"Patroon eindigt op een losse backslash en is geen geldige regex: {p}")
    if "'" not in p:
        return f"r'{p}'"
    if '"' not in p:
        return f'r"{p}"'
    raise ValueError(f"Patroon bevat zowel ' als \" en kan niet in SQL: {p}")


def _str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def whole_weeks(start: date, end: date) -> tuple[date, date]:
    """Clip to whole Monday-Sunday weeks, so the first and last week are complete."""
    from datetime import timedelta

    s = start + timedelta(days=(7 - start.weekday()) % 7)
    e = end - timedelta(days=(end.weekday() + 1) % 7)
    return (s, e) if s <= e else (start, end)


def _where(f: SourceFilters) -> str:
    start, end = whole_weeks(f.start, f.end) if f.granularity == "week" else (f.start, f.end)
    lines = [f"data_date BETWEEN {_str(str(start))} AND {_str(str(end))}"]
    if f.search_type:
        lines.append(f"search_type = {_str(f.search_type.upper())}")
    if f.countries:
        lines.append(f"country IN ({', '.join(_str(c.lower()) for c in f.countries)})")
    if f.devices:
        lines.append(f"device IN ({', '.join(_str(d.upper()) for d in f.devices)})")
    if f.include_patterns:
        ors = " OR ".join(f"REGEXP_CONTAINS(url, {_raw(p)})" for p in f.include_patterns)
        lines.append(f"({ors})")
    for p in f.exclude_patterns:
        lines.append(f"NOT REGEXP_CONTAINS(url, {_raw(p)})")
    return "\n    AND ".join(lines)


def _period(f: SourceFilters) -> str:
    return "DATE_TRUNC(data_date, WEEK(MONDAY))" if f.granularity == "week" else "data_date"


def grouping_sql(f: SourceFilters) -> str:
    """url × period for the pre-period: input for building the groups."""
    head = (
        "-- Stap 1: url-data voor de groepsindeling (pre-periode)\n"
        "-- Draai in BigQuery en plak het resultaat terug, of download als CSV.\n"
    )
    if not f.min_impressions:
        return head + f"""SELECT
  url,
  {_period(f)} AS date,
  SUM(clicks) AS clicks,
  SUM(impressions) AS impressions,
  SUM(sum_position) AS sum_position
FROM `{f.full_table()}`
WHERE {_where(f)}
GROUP BY url, date
ORDER BY url, date"""
    # Minimum per url: first sum per url × period, then keep urls above the bar.
    # (An aggregate inside a window in QUALIFY is not allowed in BigQuery.)
    where = _where(f)
    return head + f"""WITH daily AS (
  SELECT
    url,
    {_period(f)} AS date,
    SUM(clicks) AS clicks,
    SUM(impressions) AS impressions,
    SUM(sum_position) AS sum_position
  FROM `{f.full_table()}`
  WHERE {where}
  GROUP BY url, date
),
urls AS (
  SELECT url
  FROM daily
  GROUP BY url
  HAVING SUM(impressions) >= {int(f.min_impressions)}
)
SELECT daily.*
FROM daily
JOIN urls USING (url)
ORDER BY url, date"""


def _common_prefix(urls: list[str]) -> str:
    prefix = os.path.commonprefix(urls)
    if not prefix:
        return ""
    cut = prefix.find("/", prefix.find("//") + 2) if "//" in prefix else -1
    return prefix[:cut] if cut > 0 else ""  # keep scheme + host only


def analysis_sql(f: SourceFilters, assignments: pd.DataFrame) -> tuple[str, int]:
    """date × groep totals, with the assignment embedded as arrays.
    Returns (sql, size_in_bytes)."""
    urls = assignments["url"].astype(str).tolist()
    prefix = _common_prefix(urls)
    parts = []
    for grp, sub in assignments.groupby("groep", sort=False):
        paths = ", ".join(_str(u[len(prefix):]) for u in sub["url"].astype(str))
        parts.append(f"  SELECT p AS path, {_str(str(grp))} AS groep FROM UNNEST([{paths}]) AS p")
    union = "\n  UNION ALL\n".join(parts)
    key = f"SUBSTR(url, {len(prefix) + 1})" if prefix else "url"
    prefix_filter = f"\n    AND STARTS_WITH(url, {_str(prefix)})" if prefix else ""
    sql = f"""-- Stap 2: dagtotalen per groep voor de effectmeting
-- De groepsindeling ({nl(len(urls))} url's) zit in de query; je plakt alleen een klein resultaat terug.
WITH indeling AS (
{union}
),
gsc AS (
  SELECT
    {_period(f)} AS date,
    {key} AS path,
    clicks,
    impressions,
    sum_position
  FROM `{f.full_table()}`
  WHERE {_where(f)}{prefix_filter}
)
SELECT
  gsc.date,
  indeling.groep,
  SUM(gsc.clicks) AS clicks,
  SUM(gsc.impressions) AS impressions,
  SUM(gsc.sum_position) AS sum_position,
  COUNT(DISTINCT gsc.path) AS n_urls
FROM gsc
JOIN indeling USING (path)
GROUP BY gsc.date, indeling.groep
ORDER BY gsc.date, indeling.groep"""
    return sql, len(sql.encode("utf-8"))


def url_level_sql(f: SourceFilters) -> str:
    """Fallback when the assignment is too large to embed: url × period,
    joined to the assignment inside the app."""
    return grouping_sql(f).replace(
        "-- Stap 1: url-data voor de groepsindeling (pre-periode)",
        "-- Stap 2 (alternatief): url-data voor de effectmeting; de app koppelt de groepen",
    )
