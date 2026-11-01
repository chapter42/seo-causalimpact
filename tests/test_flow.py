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
    assert "de testgroep haalde" in text and "Placebotest" in text


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
    assert "de testgroep haalde" in " ".join(mk.value for mk in at.markdown)


def test_gcloud_login_errors_and_impersonation(monkeypatch):
    import google.auth
    import google.oauth2.credentials as oc
    from google.auth.exceptions import DefaultCredentialsError
    from core import gsc_api

    def no_login(*a, **k):
        raise DefaultCredentialsError("none")

    monkeypatch.setattr(google.auth, "default", no_login)
    try:
        gsc_api.credentials_from_gcloud("seo-x@p.iam.gserviceaccount.com")
        raise AssertionError("expected LoginError")
    except gsc_api.LoginError as exc:
        assert "gcloud auth application-default login" in str(exc)

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (oc.Credentials(token="t"), "p"))
    creds = gsc_api.credentials_from_gcloud("seo-x@p.iam.gserviceaccount.com")
    assert creds.service_account_email == "seo-x@p.iam.gserviceaccount.com"

    msg = gsc_api.explain_error(Exception("PERMISSION_DENIED: iam.serviceAccounts.getAccessToken"), "seo-x@p.iam.gserviceaccount.com")
    assert "serviceAccountTokenCreator" in msg


class _Req:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def execute(self):
        if self.exc:
            raise self.exc
        return self.resp


class _FakeBQ:
    """Minimal stand-in for the BigQuery v2 discovery client."""

    def __init__(self):
        from googleapiclient.errors import HttpError
        import httplib2
        self.not_found = HttpError(httplib2.Response({"status": 404}), b"not found")

    def projects(self):
        return self

    def datasets(self):
        return self

    def tables(self):
        return self

    def list(self, projectId=None, maxResults=None):
        if projectId is None:
            return _Req({"projects": [{"projectReference": {"projectId": p}} for p in ["gsc-export-a", "leeg"]]})
        if projectId == "gsc-export-a":
            return _Req({"datasets": [{"datasetReference": {"datasetId": d}, "location": "EU"} for d in ["searchconsole", "ga4"]]})
        return _Req({"datasets": []})

    def list_next(self, req, resp):
        return None

    def get(self, projectId, datasetId, tableId):
        if datasetId == "searchconsole":
            return _Req({"numRows": "1234"})
        return _Req(exc=self.not_found)


def test_bigquery_export_discovery():
    from core import bq_discovery
    found = bq_discovery.list_gsc_exports(None, svc=_FakeBQ())
    assert [f["table"] for f in found] == ["gsc-export-a.searchconsole.searchdata_url_impression"]
    assert found[0]["rows"] == 1234


def test_app_sidebar_pickers():
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["creds"] = type("C", (), {"service_account_email": "roy@chapter42.com"})()
    at.session_state["sites"] = ["sc-domain:voorbeeld.nl", "https://www.voorbeeld.nl/"]
    at.session_state["bq_exports"] = [{"project": "gsc-export-a", "dataset": "searchconsole",
                                       "table": "gsc-export-a.searchconsole.searchdata_url_impression",
                                       "rows": 1234, "location": "EU"}]
    at.run()
    assert not at.exception
    at.selectbox(key="bq_table").set_value("gsc-export-a.searchconsole.searchdata_url_impression").run()
    assert any("gsc-export-a.searchconsole" in t.value for t in at.text_input)
    at.radio(key="g_source").set_value("GSC API").run()
    assert not at.exception
    assert any("sc-domain:voorbeeld.nl" in c.value for c in at.caption)


def test_stale_env_key_is_ignored_for_gcloud(monkeypatch):
    import google.auth
    import google.oauth2.credentials as oc
    from core import gsc_api

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "./credentials/bestaat-niet.json")
    seen = {}

    def fake_default(*a, **k):
        import os
        seen["env"] = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        return oc.Credentials(token="t"), "p"

    monkeypatch.setattr(google.auth, "default", fake_default)
    gsc_api.credentials_from_gcloud(None)
    assert seen["env"] is None


def test_grouping_sql_shape():
    # Validated with BigQuery dry runs (Nov 2026). Guards against the old
    # QUALIFY SUM(SUM()) form, which BigQuery rejects.
    f = sb.SourceFilters(table="p.d", start=date(2026, 8, 6), end=date(2026, 10, 29),
                         include_patterns=["it's", "/blog/"], exclude_patterns=["/p/"], min_impressions=100)
    sql = sb.grouping_sql(f)
    assert "QUALIFY" not in sql and "SUM(SUM(" not in sql
    assert "HAVING SUM(impressions) >= 100" in sql
    assert "r'/blog/'" in sql and "r\"it's\"" in sql and "'''" not in sql


def test_two_sided_significance_matches_interval():
    long, res = _groups()
    w0 = dio.group_series(long, res.assignments)[["test", "controle"]]
    k = im.run_impact(w0, "2026-09-01", "2026-09-07", alpha=0.10).kpis
    excludes_zero = k["rel_effect_lower"] > 0 or k["rel_effect_upper"] < 0
    assert k["significant"] == excludes_zero


def test_duration_advice():
    from core import duration as du
    _, res = _groups()
    curve = du.combined_curve(res.series, ["controle", "test"], alpha=0.10)
    assert list(curve["weken"]) == list(range(2, 13))
    assert (curve["mde"] > 0).all()
    adv = du.advise(curve, 0.05)
    assert adv["haalbaar"] and adv["weken"] >= du.MIN_WEEKS
    assert not du.advise(curve, 0.0001)["haalbaar"]


def test_app_duration_and_interim():
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.radio(key="g_source").set_value("Demodata").run()
    at.button[[b.label for b in at.button].index("Laad demodata")].click().run()
    at.button[[b.label for b in at.button].index("▶︎ Maak groepen")].click().run()
    assert not at.exception
    assert any("Advies:" in s.value for s in at.success)
    at.sidebar.radio[0].set_value("2 · Effect meten").run()
    at.radio(key="a_source").set_value("Demodata").run()
    at.button[[b.label for b in at.button].index("Laad demodata")].click().run()
    at.date_input[2].set_value(date(2026, 10, 20)).run()
    at.date_input[3].set_value(date(2026, 10, 20)).run()
    at.button[[b.label for b in at.button].index("▶︎ Analyse draaien")].click().run()
    assert not at.exception
    assert any("Tussenstand" in i.value for i in at.info)


def test_voorbeelddata_walkthrough():
    """Follows voorbeelddata/LEESMIJ.md step by step."""
    vd = Path(APP).parent / "voorbeelddata"
    at = AppTest.from_file(APP, default_timeout=300).run()
    at.radio(key="g_source").set_value("BigQuery (query + plakken)").run()
    at.date_input(key="g_start").set_value(date(2026, 6, 1))
    at.date_input(key="g_end").set_value(date(2026, 8, 30))
    at.text_area(key="g_pt").input("pdp = /p/\nlijst = /l/\ninfo = /inf/")
    at.text_area(key="g_bq_paste").input((vd / "stap1_pre_periode.csv").read_text()).run()
    at.button(key="g_bq_go").click().run()
    at.button[[b.label for b in at.button].index("▶︎ Maak groepen")].click().run()
    assert not at.exception
    mine = at.session_state["g_result"].assignments[["url", "groep"]].sort_values("url").reset_index(drop=True)
    ref = pd.read_csv(vd / "indeling.csv")[["url", "groep"]].sort_values("url").reset_index(drop=True)
    assert mine.equals(ref)

    at.sidebar.radio[0].set_value("2 · Effect meten").run()
    at.text_area(key="a_bq_paste").input((vd / "stap2_groepstotalen_met_effect.csv").read_text()).run()
    at.button(key="a_bq_go").click().run()
    at.date_input(key="a_live").set_value(date(2026, 8, 31)).run()
    at.date_input(key="a_post").set_value(date(2026, 9, 7)).run()
    at.button[[b.label for b in at.button].index("▶︎ Analyse draaien")].click().run()
    k = at.session_state["a_result"][0].kpis
    assert k["significant"] and 0.04 < k["rel_effect"] < 0.07

    at.date_input(key="a_end").set_value(date(2026, 9, 20)).run()
    assert at.date_input(key="a_live").value == date(2026, 8, 31)  # choice survives
    at.button[[b.label for b in at.button].index("▶︎ Analyse draaien")].click().run()
    assert any("Tussenstand: dag 14 van 21" in i.value for i in at.info)
