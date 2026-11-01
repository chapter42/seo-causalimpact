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


ACCOUNTS_FILE = "serviceaccounts.txt"  # local, git-ignored: "klant = sa@project.iam.gserviceaccount.com"


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
                                  help=f"Klanten en serviceaccounts staan in {ACCOUNTS_FILE}.")
            if choice == "(ander serviceaccount…)":
                sa = st.text_input("Serviceaccount", placeholder="seo@klant-project.iam.gserviceaccount.com").strip() or None
            elif choice != "(eigen gcloud-account)":
                sa = accounts[choice]
                st.caption(sa)
            creds_fn = lambda: gsc_api.credentials_from_gcloud(sa)  # noqa: E731
        else:
            up = st.file_uploader("Sleutel (JSON)", type=["json"], key="sa_upload")
            default_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials/service-account.json")
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


def filters_ui(prefix: str, defaults: dict, pre_label: str) -> tuple[sb.SourceFilters, list]:
    """Shared selection widgets. Returns the filters and the page types."""
    c1, c2, c3 = st.columns(3)
    with c1:
        start = st.date_input(f"{pre_label}: start", value=defaults["start"], key=f"{prefix}start", format="DD-MM-YYYY")
        end = st.date_input(f"{pre_label}: eind", value=defaults["end"], key=f"{prefix}end", format="DD-MM-YYYY")
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
        "start": today - timedelta(days=3 + 12 * 7), "end": today - timedelta(days=3),
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
        names = [n.strip() for n in st.text_input("Groepen (eerste = controle)", "controle, test").split(",") if n.strip()]
        seed = st.number_input("Seed", 0, value=42, help="Zelfde data + zelfde seed = zelfde indeling.")
    with c2:
        bins = st.slider("Verkeersklassen per paginatype", 2, 10, 4)
        use_trend = st.checkbox("Stratificeer ook op trend", True)
    with c3:
        min_clicks = st.number_input("Min. klikken per url (pre-periode)", 0, value=10, step=5)
        max_share = st.number_input("Max. aandeel per url (%)", 0.0, 100.0, 2.0, 0.5,
                                    help="Url's die meer dan dit deel van alle klikken halen zijn uitschieters en gaan eruit.")
    with c4:
        per = "dagen" if dio.infer_granularity(long["date"]) == "day" else "weken"
        st.select_slider("Zekerheid", [80, 90, 95], value=90, key="g_zekerheid", format_func=lambda v: f"{v}%",
                         help="Hoe zeker je wilt zijn voordat je een effect echt noemt. Voor SEO is 80 tot 90% gangbaar. "
                              "Geldt voor het looptijdadvies en (via testconfig.json) voor de analyse in stap 2.")
        attempts = st.number_input("Max. pogingen", 1, 500, 50)
    with st.expander("Drempels voor de toetsen"):
        t1, t2, t3, t4 = st.columns(4)
        smd_max = t1.number_input("Max. |SMD|", 0.01, 0.5, 0.10, 0.01, help="Gestandaardiseerd verschil per kenmerk; < 0,1 is gangbaar.")
        corr_min = t2.number_input("Min. correlatie reeksen", 0.0, 1.0, 0.90, 0.01)
        trend_p = t3.number_input("Min. p parallelle trend", 0.0, 1.0, 0.20, 0.05)
        placebo = t4.number_input("Max. placebo-effect (%)", 0.0, 50.0, 3.0, 0.5)

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
            "start": str(f.start), "end": str(f.end), "countries": f.countries, "devices": f.devices,
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
    groups = list(dict.fromkeys(assignments["groep"]))
    st.caption(" · ".join(f"{g}: {nl((assignments['groep'] == g).sum())} url's" for g in groups))

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
                st.info("Demo: livegang 1 september, uitrol t/m 6 september, meten vanaf 7 september.")

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
    cols = list(wide.columns)
    c1, c2, c3 = st.columns(3)
    with c1:
        treated = st.selectbox("Testgroep", cols, index=1 if len(cols) > 1 else 0)
        controls = st.multiselect("Controle", [c for c in cols if c != treated],
                                  default=[c for c in cols if c != treated][:1],
                                  help="Normaal alleen 'controle'. Andere testgroepen zijn zelf beïnvloed en horen hier niet.")
    idx = wide.index
    test_cfg = (cfg or {}).get("test", {})

    def _in_range(d):
        try:
            d = date.fromisoformat(d)
        except (TypeError, ValueError):
            return None
        return d if idx.min().date() <= d <= idx.max().date() else None

    default_live = _in_range(test_cfg.get("livegang")) or idx[int(len(idx) * 0.66)].date()
    with c2:
        live = st.date_input("Livegang (start uitrol)", value=default_live, min_value=idx.min().date(),
                             max_value=idx.max().date(), format="DD-MM-YYYY", key=f"a_live_{default_live}")
        default_post = _in_range(test_cfg.get("meten_vanaf")) or live
        default_post = max(default_post, live)
        post = st.date_input("Meten vanaf", value=default_post, min_value=live, max_value=idx.max().date(), format="DD-MM-YYYY",
                             key=f"a_post_{default_post}",
                             help="Dagen tussen livegang en deze datum (uitrol, recrawl) blijven buiten de analyse.")
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
                pl = im.run_placebo(data, str(live), engine=engine, alpha=alpha) if do_placebo else None
        except Exception as exc:  # noqa: BLE001
            st.error(f"Analyse mislukt: {exc}")
            return
        meta = {
            "zekerheid": zek,
            "advies_weken": ((cfg or {}).get("looptijd") or {}).get("advies_weken"),
            "cfg": cfg, "live": str(live), "post": str(post),
        }
        ss.a_result = (res, pl, treated, metric or "waarde", meta)

    if "a_result" in ss:
        show_impact(*ss.a_result)


def long_to_wide(long: pd.DataFrame, assignments: pd.DataFrame) -> dict:
    rate = dio.match_rate(long, assignments)
    (st.warning if rate < 0.8 else st.caption)(f"{nl_pct(rate, sign=False)} van de url's uit de indeling gevonden in de data.")
    return {m: dio.group_series(long, assignments, m) for m in ["clicks", "impressions"]}


def table_to_wide(tbl: pd.DataFrame, assignments: pd.DataFrame):
    try:
        shape = dio.detect_shape(tbl)
        if shape == "long":
            return long_to_wide(dio.to_long(tbl), assignments)
        if shape == "group":
            g = dio.to_group_daily(tbl)
            return {m: g.pivot_table(index="date", columns="groep", values=m, aggfunc="sum").sort_index().fillna(0)
                    for m in ["clicks", "impressions"] if g[m].notna().any()}
        t = dio.standardise_columns(tbl)
        if "date" not in t:
            st.error("Geen datumkolom gevonden.")
            return None
        t["date"] = pd.to_datetime(t["date"])
        return t.set_index("date").sort_index().apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
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
        txt += (f"\n\n⚠️ **De placebotest slaat aan** ({nl_pct(pl.kpis['rel_effect'])} op een nepdatum). "
                "Test- en controlegroep liepen al uiteen vóór de ingreep; het resultaat is daardoor onbetrouwbaar.")
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
        updated = {**cfg, "zekerheid": zek, "test": {**cfg.get("test", {}), "livegang": meta["live"], "meten_vanaf": meta["post"]}}
        st.download_button(
            "⬇︎ Testconfig bijwerken (met livegang en meetstart)", dio.dump_config(updated), "testconfig.json",
            "application/json", width="stretch",
            help="Vervang je oude testconfig.json hiermee. Bij de volgende tussenstand staan de datums dan meteen goed.",
        )


# =============================================================================
# Layout
# =============================================================================
st.sidebar.title("📈 SEO-test")
step = st.sidebar.radio("Stap", ["1 · Groepen maken", "2 · Effect meten"], label_visibility="collapsed")
service_account_ui()
st.sidebar.caption("Model: statsmodels standaard; tfcausalimpact " + ("beschikbaar" if im.tfci_available() else "niet geïnstalleerd"))

if step.startswith("1"):
    step_groups()
else:
    step_analysis()
