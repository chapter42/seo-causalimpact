"""End-to-end smoke tests: core modules and the Streamlit app (headless).

Run:  python -m pytest -q tests
"""

from datetime import date
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from core import data_io as dio
from core import grouping as grp
from core import impact as im
from core import sql_builder as sb

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def _groups():
    long = dio.make_demo_long()
    pre = long[long["date"] < "2026-08-01"]
    res = grp.make_groups(pre, grp.GroupingConfig(page_types=[("pdp", "/p/"), ("lijst", "/l/")]))
    return long, res


def test_grouping_balanced():
    _, res = _groups()
    assert res.passed
    counts = res.assignments["groep"].value_counts()
    assert abs(counts["controle"] - counts["test"]) <= 1
    assert res.balance["smd"].abs().max() < 0.1


def test_impact_recovers_effect_and_null():
    long, res = _groups()
    eff = dio.apply_demo_effect(long, res.assignments, "test", "2026-09-07", 0.08)
    w = dio.group_series(eff, res.assignments)[["test", "controle"]]
    k = im.run_impact(w, "2026-09-01", "2026-09-07").kpis
    assert k["significant"] and 0.05 < k["rel_effect"] < 0.11
    w0 = dio.group_series(long, res.assignments)[["test", "controle"]]
    k0 = im.run_impact(w0, "2026-09-01", "2026-09-07").kpis
    assert not k0["significant"]


def test_parse_paste_formats():
    tsv = "url\tdate\tclicks\timpressions\tsum_position\nhttps://a.nl/p/1\t2026-01-01\t3\t40\t120.5\n"
    assert dio.detect_shape(dio.parse_table(tsv)) == "long"
    semi = "data_date;groep;clicks;impressions\n2026-01-01;controle;1.234;20.000,5\n"
    g = dio.to_group_daily(dio.parse_table(semi))
    assert g["clicks"].iloc[0] == 1234 and g["impressions"].iloc[0] == 20000.5
    js = '[{"date":"2026-01-01","groep":"test","clicks":5,"impressions":50}]'
    assert dio.detect_shape(dio.parse_table(js)) == "group"


def test_analysis_sql_roundtrip():
    _, res = _groups()
    f = sb.SourceFilters(table="p.d", start=date(2026, 5, 4), end=date(2026, 10, 31), include_patterns=["/p/"])
    sql, size = sb.analysis_sql(f, res.assignments)
    assert "UNNEST" in sql and "STARTS_WITH(url, 'https://www.voorbeeld.nl')" in sql
    assert size < sb.MAX_QUERY_BYTES


def test_app_demo_flow():
    at = AppTest.from_file(APP, default_timeout=120).run()
    assert not at.exception
    at.radio(key="g_source").set_value("Demodata").run()
    at.button[[b.label for b in at.button].index("Laad demodata")].click().run()
    at.button[[b.label for b in at.button].index("▶︎ Maak groepen")].click().run()
    assert not at.exception
    assert any("toetsen" in s.value for s in at.success) or at.warning

    at.sidebar.radio[0].set_value("2 · Effect meten").run()
    at.radio(key="a_source").set_value("Demodata").run()
    at.button[[b.label for b in at.button].index("Laad demodata")].click().run()
    at.date_input[2].set_value(date(2026, 9, 1)).run()
    at.date_input[3].set_value(date(2026, 9, 7)).run()
    at.button[[b.label for b in at.button].index("▶︎ Analyse draaien")].click().run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "De testgroep haalde" in text and "Placebotest" in text


def test_app_paste_bigquery_group_result():
    _, res = _groups()
    long = dio.make_demo_long()
    eff = dio.apply_demo_effect(long, res.assignments, "test", "2026-09-07", 0.08)
    m = eff.merge(res.assignments[["url", "groep"]], on="url")
    out = m.groupby(["date", "groep"], as_index=False)[["clicks", "impressions"]].sum()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    pasted = out.to_csv(index=False, sep="\t")

    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["g_result"] = res
    at.session_state["g_filters"] = sb.SourceFilters(start=date(2026, 5, 4), end=date(2026, 7, 31))
    at.session_state["g_page_types"] = []
    at.run()
    at.sidebar.radio[0].set_value("2 · Effect meten").run()
    at.text_area(key="a_bq_paste").input(pasted).run()
    at.button(key="a_bq_go").click().run()
    assert not at.exception
    at.date_input[2].set_value(date(2026, 9, 1)).run()
    at.date_input[3].set_value(date(2026, 9, 7)).run()
    at.button[[b.label for b in at.button].index("▶︎ Analyse draaien")].click().run()
    assert not at.exception
    assert "De testgroep haalde" in " ".join(mk.value for mk in at.markdown)
