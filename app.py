"""SEO-testen met CausalImpact: groepen maken vooraf en effect meten achteraf.

Runnen:  streamlit run app.py
"""

from __future__ import annotations

import io
import json
import os
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st

from core import data_io as dio
from core import grouping as grp
from core import impact as im
from core import sql_builder as sb
from core.data_io import nl, nl_pct

st.set_page_config(page_title="SEO-test · CausalImpact", page_icon="📈", layout="wide")
ss = st.session_state

# Streamlit forgets widget values when a widget is not drawn (e.g. step 1 while
# you are in step 2). Re-assigning the values of the step that is NOT drawn in
# this run keeps them. (Re-assigning a widget that is drawn in the same run
# would trigger a Streamlit warning, so the visible step is left alone.)
# Buttons and file uploaders cannot be set this way, so they are skipped.
_hidden = "a_" if str(ss.get("step", "1")).startswith("1") else "g_"
for _k in list(ss.keys()):
    if isinstance(_k, str) and _k.startswith(_hidden) and not _k.endswith(("_go", "_up")) \
            and _k not in ("a_assign", "a_cfg"):
        ss[_k] = ss[_k]

COUNTRIES = ["nld", "bel", "deu", "fra", "gbr", "usa", "esp", "ita", "aut", "che", "lux", "dnk", "swe", "pol"]
DEVICES = ["DESKTOP", "MOBILE", "TABLET"]
SEARCH_TYPES = ["WEB", "IMAGE", "VIDEO", "NEWS", "DISCOVER"]


# =============================================================================
# Helpers
# =============================================================================
def lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]


def parse_page_types(text: str) -> list[tuple[str, str]]:
    out = []
    for ln in lines(text):
        if "=" not in ln:
            raise ValueError(f"Paginatype zonder '=': {ln}  (verwacht: label = regex)")
        label, pattern = ln.split("=", 1)
        out.append((label.strip(), pattern.strip()))
    return out


def page_types_text(pts: list) -> str:
    return "\n".join(f"{a} = {b}" for a, b in pts)


def fmt_table(df: pd.DataFrame, fmt: dict) -> "pd.io.formats.style.Styler":
    return df.style.format(fmt, na_rep="–")


ROOT = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(ROOT, "serviceaccounts.txt")  # local, git-ignored: "klant = sa@project.iam.gserviceaccount.com"


def load_accounts() -> dict[str, str]:
    """Known service accounts per client from serviceaccounts.txt (optional)."""
    out = {}
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    name, email = ln.split("=", 1)
                    out[name.strip()] = email.strip()
    except FileNotFoundError:
        pass
    return out


def service_account_ui() -> None:
    """Sidebar block: log in for the GSC API. Default: gcloud login on this machine,
    acting as a chosen service account (no key file). Fallback: a JSON key."""
    from core import gsc_api

    with st.sidebar.expander("🔑 Inloggen (GSC API)", expanded="creds" not in ss):
        method = st.radio("Methode", ["gcloud-login", "JSON-key"], horizontal=True, key="auth_method",
                          help="gcloud-login: één keer `gcloud auth application-default login`, geen sleutelbestanden. "
                               "JSON-key: voor plekken zonder gcloud.")
        creds_fn = None
        sa = None
        if method == "gcloud-login":
            accounts = load_accounts()
            choice = st.selectbox("Handelen als", ["(eigen gcloud-account)", *accounts, "(ander serviceaccount…)"],
                                  help="Klanten en serviceaccounts staan in serviceaccounts.txt in de app-map.")
            if choice == "(ander serviceaccount…)":
                sa = st.text_input("Serviceaccount", placeholder="seo@klant-project.iam.gserviceaccount.com").strip() or None
            elif choice != "(eigen gcloud-account)":
                sa = accounts[choice]
                st.caption(sa)
            creds_fn = lambda: gsc_api.credentials_from_gcloud(sa)  # noqa: E731
        else:
            up = st.file_uploader("Sleutel (JSON)", type=["json"], key="sa_upload")
            default_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", os.path.join(ROOT, "credentials", "service-account.json"))
            path = st.text_input("…of pad naar sleutel", value=default_path)
            creds_fn = lambda: gsc_api.credentials_from_json(up.getvalue()) if up else gsc_api.credentials_from_file(path)  # noqa: E731

        if st.button("Verbinden", width="stretch"):
            try:
                ss.creds = creds_fn()
                ss.sites = gsc_api.list_sites(ss.creds)
            except Exception as exc:  # noqa: BLE001
                ss.pop("creds", None)
                st.error(gsc_api.explain_error(exc, sa))
            else:
                ss.bq_exports, ss.bq_error = [], None
                box = st.empty()
                try:
                    from core import bq_discovery
                    ss.bq_exports = bq_discovery.list_gsc_exports(ss.creds, progress=lambda m: box.caption(m))
                except Exception as exc:  # noqa: BLE001
                    ss.bq_error = gsc_api.explain_error(exc, sa)
                box.empty()
        if "creds" in ss:
            st.caption(f"Actief: {getattr(ss.creds, 'service_account_email', '')}")
        st.caption("Er wordt niets opgeslagen; de login geldt voor deze sessie.")

    if "creds" in ss:
        sites = ss.get("sites", [])
        st.sidebar.selectbox(
            f"Property ({len(sites)})", sites, key="site",
            help="Gebruikt door de bron GSC API in stap 1 en 2.",
        )
        if not sites:
            st.sidebar.caption("Voeg het account toe in Search Console (Instellingen → Gebruikers en rechten).")
        exports = ss.get("bq_exports", [])
        labels = {e["table"]: f"{e['project']} · {e['dataset']} ({nl(e['rows'])} rijen)" for e in exports}
        st.sidebar.selectbox(
            f"BigQuery-export ({len(exports)})", ["", *labels], key="bq_table",
            format_func=lambda t: labels.get(t, "(zelf invullen)"),
            help="Datasets met een GSC-bulkexport. Vult het tabelveld bij de bron BigQuery.",
        )
        if ss.get("bq_error"):
            st.sidebar.caption("BigQuery doorzoeken lukte niet: " + ss.bq_error.splitlines()[0])


LAG_DAYS = 3  # GSC data is final after about 3 days
PRE_OPTIONS = [28, 30, 60, 90]


def period_ui(prefix: str, defaults: dict, analysis: bool) -> tuple[date, date]:
    """Periods without date pickers.

    Step 1: the last 28/30/60/90 days, ending LAG_DAYS ago.
    Step 2: from the start of the pre-period (testconfig) up to the latest final day.
    A custom range stays possible but tucked away.
    """
    end_default = date.today() - timedelta(days=LAG_DAYS)
    if not analysis:
        choice = st.radio("Pre-periode", [*PRE_OPTIONS, "eigen"], index=PRE_OPTIONS.index(90), horizontal=True,
                          key=f"{prefix}period", format_func=lambda v: f"{v} dagen" if v != "eigen" else "Eigen periode",
                          help="Tot en met 3 dagen geleden (dan is GSC-data definitief). Meer dagen geeft betere groepen; "
                               "28 dagen is krap voor het looptijdadvies.")
        if choice != "eigen":
            start, end = end_default - timedelta(days=int(choice) - 1), end_default
            st.caption(f"{start:%d-%m-%Y} t/m {end:%d-%m-%Y}")
            return start, end
    else:
        start, end = defaults["start"], end_default
        custom = st.checkbox("Andere periode", key=f"{prefix}custom_period",
                             help="Standaard: vanaf de start van de pre-periode (testconfig) tot de laatste definitieve dag.")
        if not custom:
            st.caption(f"Analyseperiode: {start:%d-%m-%Y} t/m {end:%d-%m-%Y}")
            return start, end
    start = st.date_input("Van", value=defaults["start"], key=f"{prefix}start", format="DD-MM-YYYY")
    end = st.date_input("Tot en met", value=defaults["end"] if not analysis else end_default, key=f"{prefix}end", format="DD-MM-YYYY")
    return start, end


def filters_ui(prefix: str, defaults: dict, pre_label: str) -> tuple[sb.SourceFilters, list]:
    """Shared selection widgets. Returns the filters and the page types."""
    c1, c2, c3 = st.columns(3)
    with c1:
        start, end = period_ui(prefix, defaults, analysis=pre_label != "Pre-periode")
        gran = st.radio("Detailniveau", ["day", "week"], index=["day", "week"].index(defaults["granularity"]),
                        format_func={"day": "Dag", "week": "Week"}.get, horizontal=True, key=f"{prefix}gran",
                        help="Dag geeft betere toetsen. Week maakt de export ~7× kleiner.")
    with c2:
        countries = st.multiselect("Landen (ISO-3)", sorted(set(COUNTRIES + defaults["countries"])),
                                   default=defaults["countries"], key=f"{prefix}countries")
        extra = st.text_input("Overige landen (komma's)", key=f"{prefix}extra_c")
        countries += [c.strip().lower() for c in extra.split(",") if c.strip()]
        devices = st.multiselect("Apparaten (leeg = alle)", DEVICES, default=defaults["devices"], key=f"{prefix}dev")
        stype = st.selectbox("Zoektype", SEARCH_TYPES, index=SEARCH_TYPES.index(defaults["search_type"]), key=f"{prefix}stype")
    with c3:
        inc = st.text_area("Url-patronen opnemen (regex, één per regel)", value="\n".join(defaults["include"]),
                           height=90, key=f"{prefix}inc", help="Een url telt mee als hij op minstens één patroon matcht.")
        exc = st.text_area("Url-patronen uitsluiten", value="\n".join(defaults["exclude"]), height=68, key=f"{prefix}exc")
    pts_txt = st.text_area(
        "Paginatypes voor de stratificatie (label = regex, eerste match telt)",
        value=page_types_text(defaults["page_types"]), height=80, key=f"{prefix}pt",
        help="Bijvoorbeeld:\npdp = /p/\nlijst = /l/\nWat nergens op matcht wordt 'overig'.",
    )
    f = sb.SourceFilters(
        table=defaults["table"], start=start, end=end, countries=countries, devices=devices,
        search_type=stype, include_patterns=lines(inc), exclude_patterns=lines(exc),
        granularity=gran, min_impressions=defaults["min_impressions"],
    )
    return f, parse_page_types(pts_txt)


def default_filters(cfg: dict | None = None, analysis: bool = False) -> dict:
    today = date.today()
    d = {
        "start": today - timedelta(days=LAG_DAYS + (89 if not analysis else 180)), "end": today - timedelta(days=LAG_DAYS),
        "granularity": "day", "countries": ["nld"], "devices": [], "search_type": "WEB",
        "include": [], "exclude": [], "page_types": [],
        "table": "project.searchconsole.searchdata_url_impression", "min_impressions": 0,
    }
    if cfg:
        src = cfg.get("bron", {})
        d.update({
            "countries": src.get("countries", d["countries"]), "devices": src.get("devices", []),
            "search_type": src.get("search_type", "WEB"), "include": src.get("include_patterns", []),
            "exclude": src.get("exclude_patterns", []), "table": src.get("table", d["table"]),
            "granularity": src.get("granularity", "day"),
            "page_types": [tuple(x) for x in cfg.get("paginatypes", [])],
        })
        d["start"] = date.fromisoformat(src["start"]) if src.get("start") else d["start"]
        if not analysis and src.get("end"):
            d["end"] = date.fromisoformat(src["end"])
    return d


def paste_or_upload(key: str, help_text: str) -> pd.DataFrame | None:
    """Paste box + uploader. Returns the parsed table after pressing the button."""
    c1, c2 = st.columns([3, 2])
    with c1:
        pasted = st.text_area("Plak het resultaat", height=140, key=f"{key}_paste", help=help_text,
                              placeholder="Kopieer de resultaten in BigQuery (CSV/TSV/JSON) en plak ze hier.")
    with c2:
        up = st.file_uploader("…of upload het bestand", type=["csv", "tsv", "txt", "json"], key=f"{key}_up")
    if st.button("Inlezen", key=f"{key}_go", type="primary"):
        raw = up.getvalue() if up else pasted
        if not raw:
            st.warning("Niets geplakt of geüpload.")
            return None
        try:
            return dio.parse_table(raw)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Kon de tabel niet lezen: {exc}")
    return None


def fetch_api(f: sb.SourceFilters, key: str) -> pd.DataFrame | None:
    from core import gsc_api
    if "creds" not in ss:
        st.info("Log eerst in via de zijbalk (🔑 Inloggen).")
        return None
    site = ss.get("site")
    if site:
        st.caption(f"Property: **{site}** (wijzigen in de zijbalk)")
    else:
        st.warning("Kies eerst een property in de zijbalk.")
    fresh = st.checkbox("Ook verse (nog niet definitieve) data", key=f"{key}_fresh",
                        help="Standaard alleen definitieve data; verse data van de laatste dagen kan nog wijzigen.")
    if st.button("Ophalen via GSC API", key=f"{key}_go", type="primary", disabled=not site):
        box = st.empty()
        try:
            long = gsc_api.fetch_url_day(
                ss.creds, site, f.start, f.end, countries=f.countries, devices=f.devices,
                search_type=f.search_type, include_patterns=f.include_patterns,
                exclude_patterns=f.exclude_patterns, data_state="all" if fresh else "final",
                progress=lambda m: box.caption(m),
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Ophalen mislukt: {exc}")
            return None
        box.empty()
        if long.empty:
            st.warning("Geen data. Check property, periode en patronen.")
            return None
        ss[f"{key}_site_used"] = site  # stored in testconfig.json
        capped = long.attrs.get("capped_days") or []
        if capped:
            st.warning(f"Op {len(capped)} dag(en) zit de API aan zijn maximum van 50.000 rijen (bijv. {capped[0]}). "
                       "Url's met weinig verkeer kunnen dan ontbreken. Gebruik voor deze site liever de BigQuery-route.")
        return dio.to_period(long, f.granularity)
    return None


def data_summary(long: pd.DataFrame) -> None:
    c = st.columns(4)
    c[0].metric("Url's", nl(long["url"].nunique()))
    c[1].metric("Klikken", nl(long["clicks"].sum()))
    c[2].metric("Vertoningen", nl(long["impressions"].sum()))
    c[3].metric("Periodes", f"{nl(long['date'].nunique())} ({dio.infer_granularity(long['date'])})")


# =============================================================================
# Step 1: groups
# =============================================================================
def step_groups() -> None:
    st.header("1 · Groepen maken")
    st.caption("Kies welke url's meedoen, haal de pre-periode op en verdeel ze gestratificeerd over een "
               "controlegroep en testgroep(en). De app toetst of de groepen vooraf gelijk genoeg zijn.")

    st.subheader("Selectie")
    source = st.radio("Bron", ["BigQuery (query + plakken)", "GSC API", "Demodata"],
                      horizontal=True, key="g_source")
    defaults = default_filters()
    if source.startswith("BigQuery"):
        chosen = ss.get("bq_table") or ""
        defaults["table"] = st.text_input("Tabel (project.dataset of volledige tabel)", value=chosen or defaults["table"],
                                          key=f"g_table_{chosen}", help="Kies een export in de zijbalk, of typ hem hier.")
        defaults["min_impressions"] = st.number_input("Minimaal vertoningen per url in de query", 0, value=100, step=50, key="g_minimp")
    try:
        f, page_types = filters_ui("g_", defaults, "Pre-periode")
    except ValueError as exc:
        st.error(str(exc))
        return

    long = None
    if source.startswith("BigQuery"):
        sql = sb.grouping_sql(f)
        st.code(sql, language="sql")
        st.download_button("⬇︎ Query (.sql)", sql, "stap1_groepsindeling.sql", "text/plain")
        tbl = paste_or_upload("g_bq", "Kolommen: url, date, clicks, impressions, sum_position")
        if tbl is not None:
            try:
                long = dio.to_long(tbl)
            except ValueError as exc:
                st.error(str(exc))
    elif source.startswith("GSC"):
        long = fetch_api(f, "g_api")
    else:
        if st.button("Laad demodata", type="primary"):
            demo = dio.make_demo_long()
            long = dio.to_period(demo[demo["date"] < "2026-08-01"], f.granularity)
            ss.demo_full = demo
            st.info("Demo: 600 url's (/p/, /l/, /inf/), 89 dagen. Tip: paginatypes `pdp = /p/` en `lijst = /l/`.")

    if long is not None:
        ss.g_long, ss.g_filters, ss.g_page_types = long, f, page_types
        ss.pop("g_result", None)

    if "g_long" not in ss:
        return
    long = ss.g_long
    data_summary(long)

    st.subheader("Indeling")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        names = [n.strip() for n in st.text_input("Groepen (eerste = controle)", "controle, test", key="g_names").split(",") if n.strip()]
        seed = st.number_input("Seed", 0, value=42, key="g_seed", help="Zelfde data + zelfde seed = zelfde indeling.")
    with c2:
        bins = st.slider("Verkeersklassen per paginatype", 2, 10, 4, key="g_bins")
        use_trend = st.checkbox("Stratificeer ook op trend", True, key="g_trend")
    with c3:
        min_clicks = st.number_input("Min. klikken per url (pre-periode)", 0, value=10, step=5, key="g_min_clicks")
        max_share = st.number_input("Max. aandeel per url (%)", 0.0, 100.0, 2.0, 0.5, key="g_max_share",
                                    help="Url's die meer dan dit deel van alle klikken halen zijn uitschieters en gaan eruit.")
    with c4:
        per = "dagen" if dio.infer_granularity(long["date"]) == "day" else "weken"
        st.select_slider("Zekerheid", [80, 90, 95], value=90, key="g_zekerheid", format_func=lambda v: f"{v}%",
                         help="Hoe zeker je wilt zijn voordat je een effect echt noemt. Voor SEO is 80 tot 90% gangbaar. "
                              "Geldt voor het looptijdadvies en (via testconfig.json) voor de analyse in stap 2.")
        attempts = st.number_input("Max. pogingen", 1, 500, 50, key="g_attempts")
    with st.expander("Drempels voor de toetsen"):
        t1, t2, t3, t4 = st.columns(4)
        smd_max = t1.number_input("Max. |SMD|", 0.01, 0.5, 0.10, 0.01, key="g_smd", help="Gestandaardiseerd verschil per kenmerk; < 0,1 is gangbaar.")
        corr_min = t2.number_input("Min. correlatie reeksen", 0.0, 1.0, 0.90, 0.01, key="g_corr")
        trend_p = t3.number_input("Min. p parallelle trend", 0.0, 1.0, 0.20, 0.05, key="g_trendp")
        placebo = t4.number_input("Max. placebo-effect (%)", 0.0, 50.0, 3.0, 0.5, key="g_placebo")

    if st.button("▶︎ Maak groepen", type="primary", width="stretch"):
        cfg = grp.GroupingConfig(
            group_names=names, page_types=ss.g_page_types, traffic_bins=bins, use_trend=use_trend,
            min_clicks=min_clicks, max_url_share=max_share / 100, seed=int(seed), max_attempts=int(attempts),
            smd_max=smd_max, corr_min=corr_min, trend_p_min=trend_p, placebo_max=placebo / 100,
        )
        try:
            with st.spinner("Groepen verdelen en toetsen…"):
                season = 7 if per == "dagen" else 1
                ss.g_result, ss.g_cfg = grp.make_groups(long, cfg, season=season), cfg
        except ValueError as exc:
            st.error(str(exc))
            return

    if "g_result" in ss:
        show_group_result(ss.g_result, ss.g_cfg)


def duration_advice(r: grp.GroupingResult, cfg: grp.GroupingConfig) -> tuple[dict, float, int]:
    """Looptijdadvies: shortest test length at which the expected effect is detectable."""
    from core import duration as du

    st.subheader("Looptijdadvies")
    zek = int(ss.get("g_zekerheid", 90))
    c1, c2 = st.columns([1, 3])
    expected = c1.number_input("Verwacht effect (%)", 0.5, 50.0, 5.0, 0.5, key="g_expected",
                               help="Het effect dat je realistisch verwacht van de wijziging, op klikken.") / 100
    key = (r.seed_used, zek, tuple(cfg.group_names), len(r.series), float(r.series.values.sum()))
    if ss.get("g_curve_key") != key:
        with st.spinner("Looptijd doorrekenen met het analysemodel…"):
            ss.g_curve = du.combined_curve(r.series, cfg.group_names, alpha=1 - zek / 100)
        ss.g_curve_key = key
    curve = ss.g_curve
    advice = du.advise(curve, expected)
    with c2:
        if advice["haalbaar"]:
            st.success(
                f"**Advies: {du.weeks_to_text(advice['weken'])} meten.** Dan is een effect van {nl_pct(expected, sign=False)} "
                f"met {zek}% zekerheid aantoonbaar; vanaf {nl_pct(advice['mde'], sign=False)} vind je het in 4 van de 5 gevallen."
            )
        else:
            st.warning(
                f"**Een effect van {nl_pct(expected, sign=False)} is met deze groepen niet betrouwbaar aan te tonen.** "
                f"Het beste haalbare is {nl_pct(advice['beste_mde'], sign=False)}, bij {du.weeks_to_text(advice['beste_weken'])}. "
                "Langer meten helpt niet: de voorspelling wordt verder vooruit juist onzekerder. "
                "Opties: meer url's in de test, meten op vertoningen in plaats van klikken, of alleen testen als je een groter effect verwacht."
            )
        if advice["haalbaar"] and advice["beste_weken"] > advice["weken"]:
            st.caption(f"Het scherpst meet je bij {du.weeks_to_text(advice['beste_weken'])} "
                       f"(vanaf {nl_pct(advice['beste_mde'], sign=False)}); daarna wordt de voorspelling onzekerder.")
        st.caption("Tel daar de uitrol- en recrawltijd bij op (meestal 1 tot 2 weken). "
                   "Stop niet eerder omdat een tussenstand al significant lijkt: dan meet je vooral ruis.")
    chart = pd.DataFrame({
        "Kleinste aantoonbare effect (%)": curve["mde"].to_numpy() * 100,
        "Verwacht effect (%)": expected * 100,
    }, index=pd.Index(curve["weken"], name="Looptijd (weken)"))
    st.line_chart(chart, width="stretch")
    return advice, expected, zek


def show_group_result(r: grp.GroupingResult, cfg: grp.GroupingConfig) -> None:
    a = r.assignments
    if r.passed:
        st.success(f"Indeling haalt alle toetsen (poging {r.attempt}, seed {r.seed_used}).")
    else:
        st.warning(f"Indeling haalt niet alle toetsen; best scorende gekozen (seed {r.seed_used}).")
    for n in r.notes:
        st.caption("• " + n)

    tot = a.groupby("groep").agg(urls=("url", "size"), klikken=("clicks", "sum"), vertoningen=("impressions", "sum"))
    tot = tot.reindex(cfg.group_names)
    tot["aandeel klikken"] = tot["klikken"] / tot["klikken"].sum()
    st.dataframe(fmt_table(tot, {"urls": nl, "klikken": nl, "vertoningen": nl, "aandeel klikken": lambda v: nl_pct(v, sign=False)}),
                 width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Balans per kenmerk**")
        b = r.balance.copy()
        b["ok"] = b["ok"].map({True: "✅", False: "❌"})
        st.dataframe(fmt_table(b, {"controle": lambda v: nl(v, 3), "test": lambda v: nl(v, 3), "smd": lambda v: nl(v, 3)}),
                     width="stretch", hide_index=True)
    with c2:
        st.markdown("**Toetsen op de reeksen**")
        ck = r.checks.drop(columns=["mde_indicatief"], errors="ignore")
        for col in ["ok_correlatie", "ok_trend", "ok_placebo"]:
            ck[col] = ck[col].map({True: "✅", False: "❌"})
        st.dataframe(fmt_table(ck, {
            "correlatie": lambda v: nl(v, 3), "trendverschil_per_week": nl_pct, "trend_p": lambda v: nl(v, 2),
            "placebo_effect": nl_pct,
        }), width="stretch", hide_index=True)

    st.markdown("**Groepen in de pre-periode (geïndexeerd, gemiddelde = 100)**")
    st.line_chart(r.series / r.series.mean() * 100, width="stretch")

    with st.expander("Strata"):
        st.dataframe(a.pivot_table(index="stratum", columns="groep", values="url", aggfunc="size", fill_value=0),
                     width="stretch")
    if len(r.excluded):
        with st.expander(f"Uitgesloten url's ({nl(len(r.excluded))})"):
            st.dataframe(r.excluded[["url", "clicks", "impressions", "reden"]], width="stretch", hide_index=True)

    advice, expected, zek = duration_advice(r, cfg)

    f: sb.SourceFilters = ss.g_filters
    config = {
        "aangemaakt": datetime.now().isoformat(timespec="seconds"),
        "bron": {
            "type": ss.get("g_source"), "table": f.full_table(), "property": ss.get("g_api_site_used"),
            "start": str(ss.g_long["date"].min().date()), "end": str(ss.g_long["date"].max().date()),
            "countries": f.countries, "devices": f.devices,
            "search_type": f.search_type, "include_patterns": f.include_patterns,
            "exclude_patterns": f.exclude_patterns, "granularity": f.granularity,
        },
        "paginatypes": cfg.page_types,
        "indeling": {k: v for k, v in cfg.__dict__.items() if k != "page_types"} | {"seed_used": r.seed_used, "geslaagd": r.passed},
        "aantallen": {g: int(n) for g, n in a["groep"].value_counts().items()},
        "zekerheid": zek,
        "looptijd": {"verwacht_effect": expected, "advies_weken": advice.get("weken"), "haalbaar": advice["haalbaar"]},
    }
    ss.g_config = config

    st.subheader("Export")
    st.caption("Bewaar indeling.csv en testconfig.json bij de test: stap 2 heeft ze nodig. "
               "De lijsten per groep zijn voor de uitvoering: de testgroep krijgt de wijziging, de controle blijft onaangeroerd.")
    d1, d2 = st.columns(2)
    d1.download_button("⬇︎ Indeling, alle groepen (CSV)", a.to_csv(index=False), "indeling.csv", "text/csv",
                       width="stretch", help="Url, groep, stratum en pre-cijfers. Input voor stap 2.")
    d2.download_button("⬇︎ Testconfig (JSON)", dio.dump_config(config), "testconfig.json", "application/json",
                       width="stretch", help="Alle filters en instellingen. Stap 2 vult hiermee alles in.")
    cols = st.columns(len(cfg.group_names))
    for col, g in zip(cols, cfg.group_names):
        urls = a.loc[a["groep"] == g, "url"]
        rol = "niet wijzigen" if g == cfg.group_names[0] else "wijziging doorvoeren"
        col.download_button(f"⬇︎ {g} ({nl(len(urls))} url's)", "\n".join(urls) + "\n", f"urls_{g}.txt", "text/plain",
                            width="stretch", help=f"Eén url per regel; {rol}.")
    for g in cfg.group_names:
        with st.expander(f"Url's {g} kopiëren ({nl((a['groep'] == g).sum())})"):
            st.code("\n".join(a.loc[a["groep"] == g, "url"]), language=None)


# =============================================================================
# Step 2: analysis
# =============================================================================
def step_analysis() -> None:
    st.header("2 · Effect meten")
    st.caption("Lees de indeling in, haal de data vanaf de pre-periode tot nu op en meet met CausalImpact "
               "hoeveel de testgroep afwijkt van wat de controlegroep voorspelt.")

    st.subheader("Indeling")
    have = "g_result" in ss
    use_session = st.checkbox("Gebruik de indeling uit stap 1", value=have, disabled=not have)
    c1, c2 = st.columns(2)
    up_a = c1.file_uploader("indeling.csv", type=["csv"], key="a_assign", disabled=use_session)
    up_c = c2.file_uploader("testconfig.json (vult de filters in)", type=["json"], key="a_cfg")

    cfg = dio.load_config(up_c.getvalue()) if up_c else None
    if use_session:
        assignments = ss.g_result.assignments[["url", "groep"]]
        if cfg is None and "g_config" in ss:
            cfg = ss.g_config
        if cfg is None:
            f = ss.g_filters
            cfg = {"bron": {**f.__dict__, "start": str(f.start), "end": str(f.end), "table": f.full_table()},
                   "paginatypes": ss.g_page_types}
    elif up_a:
        assignments = dio.standardise_columns(dio.parse_table(up_a.getvalue()))
        if not {"url", "groep"} <= set(assignments.columns):
            st.error("indeling.csv mist de kolommen url en groep.")
            return
        assignments = assignments[["url", "groep"]]
    else:
        st.info("Upload indeling.csv uit stap 1, of maak eerst groepen.")
        return
    groups = dio.order_groups(list(dict.fromkeys(assignments["groep"].astype(str))), cfg)
    ss.a_groups = groups
    st.caption(" · ".join(f"{g}{' (controle)' if i == 0 else ''}: {nl((assignments['groep'] == g).sum())} url's"
                          for i, g in enumerate(groups)))
    if not ((cfg or {}).get("indeling") or {}).get("group_names") and not any("contr" in g.lower() for g in groups):
        st.warning(f"Kan niet zien welke groep de controle is; ik neem '{groups[0]}'. "
                   "Upload testconfig.json uit stap 1, of kies hieronder zelf de testgroep en controle.")

    st.subheader("Data")
    source = st.radio("Bron", ["BigQuery (query + plakken)", "GSC API", "Eigen CSV", "Demodata"],
                      horizontal=True, key="a_source")
    defaults = default_filters(cfg, analysis=True)
    if source.startswith("BigQuery"):
        placeholder = default_filters()["table"]
        table_default = defaults["table"] if defaults["table"] != placeholder else (ss.get("bq_table") or placeholder)
        defaults["table"] = st.text_input("Tabel", value=table_default, key=f"a_table_{table_default}")
    cfg_prop = (cfg or {}).get("bron", {}).get("property")
    if source.startswith("GSC") and cfg_prop and cfg_prop != ss.get("site") and cfg_prop in ss.get("sites", []):
        st.button(f"Gebruik de property uit de testconfig: {cfg_prop}",
                  on_click=lambda: ss.__setitem__("site", cfg_prop))
    try:
        f, _ = filters_ui("a_", defaults, "Analyseperiode")
    except ValueError as exc:
        st.error(str(exc))
        return

    wide = None
    if source.startswith("BigQuery"):
        sql, size = sb.analysis_sql(f, assignments)
        if size > sb.MAX_QUERY_BYTES * 0.95:
            st.warning(f"De query met ingebakken indeling is {nl(size / 1e6, 1)} MB; BigQuery staat 1 MB toe. "
                       "Gebruik de url-query hieronder; de app koppelt de groepen dan zelf.")
            sql = sb.url_level_sql(f)
        st.code(sql, language="sql")
        st.download_button("⬇︎ Query (.sql)", sql, "stap2_effectmeting.sql", "text/plain")
        tbl = paste_or_upload("a_bq", "Kolommen: date, groep, clicks, impressions (of url-niveau)")
        if tbl is not None:
            wide = table_to_wide(tbl, assignments)
    elif source.startswith("GSC"):
        long = fetch_api(f, "a_api")
        if long is not None:
            wide = long_to_wide(long, assignments)
    elif source == "Eigen CSV":
        tbl = paste_or_upload("a_csv", "url × datum, datum × groep, of een brede tabel met datum + reeksen")
        if tbl is not None:
            wide = table_to_wide(tbl, assignments)
    else:
        if "demo_full" not in ss or not use_session:
            st.info("Demodata werkt met de demo-indeling uit stap 1.")
        else:
            test_g = st.selectbox("Groep met ingebouwd effect", groups[1:], key="a_demo_g")
            uplift = st.slider("Ingebouwd effect (%)", -20, 30, 8, key="a_demo_u")
            if st.button("Laad demodata", type="primary"):
                demo = dio.apply_demo_effect(ss.demo_full, assignments, test_g, "2026-09-07", uplift / 100)
                wide = long_to_wide(dio.to_period(demo, f.granularity), assignments)
                st.info("Demo: livegang 1 september, uitrolvenster 7 dagen.")

    if wide is not None:
        ss.a_wide = wide
        ss.pop("a_result", None)
    if "a_wide" not in ss:
        return
    metrics = ss.a_wide

    st.subheader("Instellingen")
    metric = st.radio("Metric", list(metrics), format_func={"clicks": "Klikken", "impressions": "Vertoningen"}.get,
                      horizontal=True) if isinstance(metrics, dict) else None
    wide = metrics[metric] if metric else metrics
    # The analysis period also applies to pasted/uploaded data (handy for an interim check).
    full_range = (wide.index.min().date(), wide.index.max().date())
    wide = wide.loc[str(f.start):str(f.end)]
    if wide.empty:
        st.error(f"Geen data binnen de analyseperiode {f.start:%d-%m-%Y} t/m {f.end:%d-%m-%Y}. "
                 f"De data loopt van {full_range[0]:%d-%m-%Y} t/m {full_range[1]:%d-%m-%Y}.")
        return
    if (wide.index.min().date(), wide.index.max().date()) != full_range:
        st.caption(f"Analyse op {wide.index.min():%d-%m-%Y} t/m {wide.index.max():%d-%m-%Y} "
                   f"(data loopt van {full_range[0]:%d-%m-%Y} t/m {full_range[1]:%d-%m-%Y}; afgekapt op de analyseperiode).")
    # Control first, in the order of the test (never alphabetical).
    order = ss.get("a_groups") or []
    cols = [c for c in order if c in wide.columns] + [c for c in wide.columns if c not in order]
    c1, c2, c3 = st.columns(3)
    with c1:
        treated = st.selectbox("Testgroep", cols, index=1 if len(cols) > 1 else 0)
        ctrl_default = [cols[0]] if cols and cols[0] != treated else [c for c in cols if c != treated][:1]
        controls = st.multiselect("Controle", [c for c in cols if c != treated], default=ctrl_default,
                                  help="Normaal alleen 'controle'. Andere testgroepen zijn zelf beïnvloed en horen hier niet.")
    idx = wide.index
    test_cfg = (cfg or {}).get("test", {})

    def _in_range(d):
        try:
            d = date.fromisoformat(d)
        except (TypeError, ValueError):
            return None
        return d if idx.min().date() <= d <= idx.max().date() else None

    lo_d, hi_d = idx.min().date(), idx.max().date()

    def _keep(key: str, fallback, low, high) -> None:
        """Keep the user's choice across reruns; fall back only when it is missing
        or falls outside the data (a value outside min/max would crash the widget)."""
        cur = ss.get(key)
        if not isinstance(cur, date) or not (low <= cur <= high):
            ss[key] = min(max(fallback, low), high)

    _keep("a_live", _in_range(test_cfg.get("livegang")) or idx[int(len(idx) * 0.66)].date(), lo_d, hi_d)
    with c2:
        live = st.date_input("Livegang (start uitrol)", min_value=lo_d, max_value=hi_d, format="DD-MM-YYYY", key="a_live")
        rollout_opts = [0, 7, 14, 21]
        if "a_rollout" not in ss:
            prev = test_cfg.get("uitrolvenster_dagen")
            ss.a_rollout = prev if prev in rollout_opts else 7
        rollout = st.radio("Uitrolvenster", rollout_opts, horizontal=True, key="a_rollout",
                           format_func=lambda d: "geen" if d == 0 else f"{d} dagen",
                           help="Tijd voor uitrol en recrawl na de livegang. Die dagen tellen niet mee. "
                                "Hele weken houden het weekpatroon zuiver.")
        post = live + timedelta(days=int(rollout))
        if post > hi_d:
            st.error(f"Livegang plus uitrolvenster ({post:%d-%m-%Y}) ligt na de laatste dag in de data ({hi_d:%d-%m-%Y}).")
            return
        st.caption(f"Meten vanaf {post:%d-%m-%Y}")
    with c3:
        engines = ["statsmodels", "tfcausalimpact"]
        engine = st.selectbox("Model", engines, help="statsmodels: snel en stabiel. tfcausalimpact: het Google-model, "
                              "draait apart met een tijdslimiet en valt terug op statsmodels als het vastloopt.")
        zek_default = int((cfg or {}).get("zekerheid", 90))
        zek = st.select_slider("Zekerheid", [80, 90, 95], value=zek_default if zek_default in (80, 90, 95) else 90,
                               format_func=lambda v: f"{v}%", key="a_zekerheid",
                               help="Hoe zeker je wilt zijn voordat je een effect echt noemt. Standaard uit testconfig.json.")
        alpha = 1 - zek / 100
        do_placebo = st.checkbox("Placebotest in de pre-periode", True)

    st.line_chart(wide[[treated, *controls]], width="stretch")

    if not controls:
        st.warning("Kies minstens één controlereeks.")
        return
    if st.button("▶︎ Analyse draaien", type="primary", width="stretch"):
        data = wide[[treated, *controls]]
        try:
            with st.spinner("Model draait…"):
                res = im.run_impact(data, str(live), str(post), engine=engine, alpha=alpha)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Analyse mislukt: {exc}")
            return
        pl = None
        ss.a_placebo_note = None
        if do_placebo:
            try:
                with st.spinner("Placebotest…"):
                    pl = im.run_placebo(data, str(live), engine=engine, alpha=alpha)
            except Exception as exc:  # noqa: BLE001 — the main result stands without a placebo
                ss.a_placebo_note = f"Placebotest overgeslagen: {exc}"
        meta = {
            "zekerheid": zek,
            "advies_weken": ((cfg or {}).get("looptijd") or {}).get("advies_weken"),
            "cfg": cfg, "live": str(live), "post": str(post),
        }
        ss.a_result = (res, pl, treated, metric or "waarde", meta)

    if "a_result" in ss:
        show_impact(*ss.a_result)


def long_to_wide(long: pd.DataFrame, assignments: pd.DataFrame) -> dict:
    rate, missing = dio.match_rate(long, assignments)
    (st.warning if rate < 0.8 else st.caption)(f"{nl_pct(rate, sign=False)} van de url's uit de indeling gevonden in de data.")
    if missing:
        with st.expander(f"Niet gevonden url's ({nl(len(missing))})"):
            st.caption("Geen data in deze periode of met deze filters. Een paar is normaal (pagina's zonder vertoningen); "
                       "veel wijst op een andere property, land of patroon dan in stap 1.")
            st.code("\n".join(missing[:500]), language=None)
    return {m: dio.group_series(long, assignments, m, ss.get("a_groups")) for m in ["clicks", "impressions"]}


def table_to_wide(tbl: pd.DataFrame, assignments: pd.DataFrame):
    try:
        shape = dio.detect_shape(tbl)
        if shape == "long":
            return long_to_wide(dio.to_long(tbl), assignments)
        if shape == "group":
            g = dio.to_group_daily(tbl)
            if dio.infer_granularity(g["date"]) == "week":
                g = g.assign(date=dio.snap_to_monday(g["date"]).to_numpy()).groupby(["date", "groep"], as_index=False).sum()
            return {m: g.pivot_table(index="date", columns="groep", values=m, aggfunc="sum").sort_index().fillna(0)
                    for m in ["clicks", "impressions"] if g[m].notna().any()}
        t = dio.standardise_columns(tbl)
        if "date" not in t:
            st.error("Geen datumkolom gevonden.")
            return None
        t["date"] = dio.to_dates(t["date"])
        if dio.infer_granularity(t["date"]) == "week":
            t["date"] = dio.snap_to_monday(t["date"]).to_numpy()
        series = {c: dio.to_number(t[c], c, integer=True) for c in t.columns if c != "date"}
        return pd.DataFrame(series, index=t["date"]).groupby(level=0).sum().sort_index().dropna(axis=1, how="all")
    except ValueError as exc:
        st.error(str(exc))
        return None


def verdict(k: dict, metric: str, pl: im.ImpactResult | None, fit: dict, unit: str, zek: int) -> str:
    rel, lo, hi = nl_pct(k["rel_effect"]), nl_pct(k["rel_effect_lower"]), nl_pct(k["rel_effect_upper"])
    n = k["n_post"]
    if k["significant"]:
        direction = "meer" if k["rel_effect"] > 0 else "minder"
        txt = (f"**Met {zek}% zekerheid een effect: de testgroep haalde {rel} {metric} ten opzichte van de verwachting** "
               f"(bandbreedte {lo} tot {hi}). Dat is {nl(k['abs_effect_cum'], sign=True)} {metric} "
               f"{direction} over {nl(n)} {unit}.")
    else:
        txt = (f"**Geen aantoonbaar effect.** De beste schatting is {rel}, maar de bandbreedte ({lo} tot {hi}) "
               f"omvat nul. Een effect kleiner dan ongeveer {nl_pct(max(abs(k['rel_effect_lower']), abs(k['rel_effect_upper'])), sign=False)} "
               "is met deze looptijd niet van ruis te onderscheiden.")
    if pl is not None and pl.kpis["significant"]:
        txt += (f"\n\n⚠️ **Let op: de placebotest slaat aan** ({nl_pct(pl.kpis['rel_effect'])} op een nepdatum vóór de livegang). "
                f"Dat gebeurt bij {zek}% zekerheid ook in een goede opzet ongeveer 1 op de {round(1 / (1 - zek / 100))} keer, "
                "maar het kan ook betekenen dat test- en controlegroep al uit elkaar liepen. Kijk in de grafiek of de lijnen "
                "vóór de livegang al uiteengaan; zo ja, vertrouw het resultaat dan niet.")
    if fit.get("corr", 1) < 0.9:
        txt += (f"\n\n⚠️ De controle voorspelt de testgroep in de pre-periode matig (correlatie {nl(fit['corr'], 2)}). "
                "Overweeg meten per week of een grotere scope.")
    return txt


def show_impact(res: im.ImpactResult, pl: im.ImpactResult | None, treated: str, metric: str, meta: dict | None = None) -> None:
    meta = meta or {}
    k = res.kpis
    label = {"clicks": "klikken", "impressions": "vertoningen"}.get(metric, metric)
    daily = im._is_daily(res.frame.index)
    unit = "dagen" if daily else "weken"
    zek = int(meta.get("zekerheid", round((1 - k["alpha"]) * 100)))

    # Interim check: shorter than the advised duration?
    weeks = meta.get("advies_weken")
    if weeks:
        done = k["n_post"] if daily else k["n_post"] * 7
        target = weeks * 7
        if done < target:
            st.info(
                f"**Tussenstand: dag {nl(done)} van {nl(target)}** (advies {weeks} weken). "
                "Dit is nog geen eindoordeel. Een effect dat nu al significant lijkt kan nog verdwijnen, "
                "en een effect dat nu ontbreekt kan nog verschijnen. Stop pas als de geadviseerde looptijd erop zit."
            )
        else:
            st.caption(f"Geadviseerde looptijd ({weeks} weken) is gehaald: dit is het eindoordeel.")
    for w in res.warnings:
        st.caption("• " + w)
    st.markdown(verdict(k, label, pl, res.pre_fit, unit, zek))

    m = st.columns(4)
    m[0].metric("Relatief effect", nl_pct(k["rel_effect"]), help=f"Bandbreedte {nl_pct(k['rel_effect_lower'])} tot {nl_pct(k['rel_effect_upper'])}")
    m[1].metric(f"Cumulatief ({label})", nl(k["abs_effect_cum"], sign=True),
                help=f"Bandbreedte {nl(k['abs_effect_cum_lower'], sign=True)} tot {nl(k['abs_effect_cum_upper'], sign=True)}")
    m[2].metric("p-waarde", nl(k["p_value"], 3),
                help=f"Kans op dit resultaat (of extremer) zonder echt effect. Significant bij p < {nl(1 - zek / 100, 2)} ({zek}% zekerheid).")
    m[3].metric("Pre-periode fit", f"r = {nl(res.pre_fit.get('corr', np.nan), 3)}",
                help=f"Gemiddelde afwijking (MAPE) {nl_pct(res.pre_fit.get('mape', np.nan), sign=False)}")

    fig = im.plot_impact(res, label)
    st.pyplot(fig, width="stretch")

    if pl is None and ss.get("a_placebo_note"):
        st.caption(ss.a_placebo_note)
    if pl is not None:
        pk = pl.kpis
        st.markdown(
            f"**Placebotest** (nepdatum {pd.Timestamp(pk['post_start']).strftime('%d-%m-%Y')}): "
            f"{nl_pct(pk['rel_effect'])}, p = {nl(pk['p_value'], 3)} → "
            + ("❌ slaat aan" if pk["significant"] else "✅ niets gevonden, zoals het hoort")
        )
        with st.expander("Placebografiek"):
            st.pyplot(im.plot_impact(pl, label, title="Placebo: nepdatum in de pre-periode"), width="stretch")

    png = io.BytesIO()
    fig.savefig(png, format="png", dpi=150, bbox_inches="tight")
    out = {**k, "testgroep": treated, "metric": metric, "pre_fit": res.pre_fit,
           "placebo": pl.kpis if pl is not None else None}
    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇︎ Grafiek (PNG)", png.getvalue(), "causal_impact.png", "image/png", width="stretch")
    d2.download_button("⬇︎ KPI's (JSON)", json.dumps(out, indent=2, default=float), "causal_impact_kpis.json",
                       "application/json", width="stretch")
    d3.download_button("⬇︎ Reeksen (CSV)", res.frame.to_csv(), "causal_impact_reeksen.csv", "text/csv", width="stretch")

    cfg = meta.get("cfg")
    if cfg:
        rollout_days = (date.fromisoformat(meta["post"]) - date.fromisoformat(meta["live"])).days
        updated = {**cfg, "zekerheid": zek, "test": {**cfg.get("test", {}), "livegang": meta["live"],
                                                     "meten_vanaf": meta["post"], "uitrolvenster_dagen": rollout_days}}
        st.download_button(
            "⬇︎ Testconfig bijwerken (met livegang en uitrolvenster)", dio.dump_config(updated), "testconfig.json",
            "application/json", width="stretch",
            help="Vervang je oude testconfig.json hiermee. Bij de volgende tussenstand staan de datums dan meteen goed.",
        )


# =============================================================================
# Layout
# =============================================================================
st.sidebar.title("📈 SEO-test")
step = st.sidebar.radio("Stap", ["1 · Groepen maken", "2 · Effect meten"], label_visibility="collapsed", key="step")
service_account_ui()
st.sidebar.caption("Model: statsmodels standaard; tfcausalimpact " + ("beschikbaar" if im.tfci_available() else "niet geïnstalleerd"))

if step.startswith("1"):
    step_groups()
else:
    step_analysis()
