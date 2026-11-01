"""Maakt voorbeelddata/ voor de app. Draaien vanuit de repo-map: .venv/bin/python scripts/maak_voorbeelddata.py"""
import json, sys
from datetime import datetime
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, ".")
from core import data_io as dio, grouping as grp, duration as du, impact as im

out = Path("voorbeelddata"); out.mkdir(exist_ok=True)
PRE_START, PRE_END = "2026-06-01", "2026-08-30"      # 13 weken
LIVE, POST = "2026-08-31", "2026-09-07"              # uitrol 1 week
END = "2026-10-25"                                    # 7 weken meten
days = (pd.Timestamp(END) - pd.Timestamp(PRE_START)).days + 1

long = dio.make_demo_long(n_urls=400, start=PRE_START, days=days, seed=2026)
long["url"] = long["url"].str.replace("www.voorbeeld.nl", "www.voorbeeldwinkel.nl", regex=False)
long["sum_position"] = long["sum_position"].round().astype(int)

def write(df, name):
    d = df.copy(); d["date"] = d["date"].dt.strftime("%Y-%m-%d")
    d.to_csv(out / name, index=False)

pre = long[long["date"] <= PRE_END]
write(pre, "stap1_pre_periode.csv")

page_types = [("pdp", "/p/"), ("lijst", "/l/"), ("info", "/inf/")]
cfg = grp.GroupingConfig(page_types=page_types)
res = grp.make_groups(pre, cfg)
assert res.passed, "demo-indeling haalt de toetsen niet"
res.assignments.to_csv(out / "indeling.csv", index=False)

zek, expected = 90, 0.05
curve = du.combined_curve(res.series, cfg.group_names, alpha=1 - zek / 100)
advice = du.advise(curve, expected)
config = {
    "aangemaakt": datetime(2026, 8, 31, 9, 0).isoformat(timespec="seconds"),
    "bron": {"type": "BigQuery (query + plakken)",
             "table": "voorbeeld-project.searchconsole.searchdata_url_impression", "property": None,
             "start": PRE_START, "end": PRE_END, "countries": ["nld"], "devices": [], "search_type": "WEB",
             "include_patterns": [], "exclude_patterns": [], "granularity": "day"},
    "paginatypes": page_types,
    "indeling": {k: v for k, v in cfg.__dict__.items() if k != "page_types"} | {"seed_used": res.seed_used, "geslaagd": res.passed},
    "aantallen": {g: int(n) for g, n in res.assignments["groep"].value_counts().items()},
    "zekerheid": zek,
    "looptijd": {"verwacht_effect": expected, "advies_weken": advice.get("weken"), "haalbaar": advice["haalbaar"]},
}
(out / "testconfig.json").write_text(dio.dump_config(config), encoding="utf-8")

with_effect = dio.apply_demo_effect(long, res.assignments, "test", POST, 0.06)
for df, suffix in [(with_effect, "met_effect"), (long, "zonder_effect")]:
    write(df, f"stap2_url_data_{suffix}.csv")
    m = df.merge(res.assignments[["url", "groep"]], on="url")
    g = m.groupby(["date", "groep"], as_index=False).agg(
        clicks=("clicks", "sum"), impressions=("impressions", "sum"),
        sum_position=("sum_position", "sum"), n_urls=("url", "nunique"))
    write(g, f"stap2_groepstotalen_{suffix}.csv")

# Self-check with the app's own analysis.
for suffix in ["met_effect", "zonder_effect"]:
    g = pd.read_csv(out / f"stap2_groepstotalen_{suffix}.csv")
    wide = dio.to_group_daily(g).pivot_table(index="date", columns="groep", values="clicks").sort_index()
    k = im.run_impact(wide[["test", "controle"]], LIVE, POST, alpha=0.10).kpis
    print(suffix, f"effect {k['rel_effect']:+.3f} ({k['rel_effect_lower']:+.3f}..{k['rel_effect_upper']:+.3f}) significant={k['significant']}")
print("advies", advice, "| url's", res.assignments["groep"].value_counts().to_dict(), "| uitgesloten", len(res.excluded))
for f in sorted(out.iterdir()):
    print(f.name, f"{f.stat().st_size/1e6:.1f} MB")
