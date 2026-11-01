"""Stratified, matched assignment of URLs to a control group and test groups,
with balance checks on the pre-period.

Approach
--------
1. Per-URL features from the pre-period: clicks, impressions, CTR, position and
   a trend (log ratio second half vs first half).
2. Strata: page type × traffic class × trend class. Strata that are too small
   fall back to a coarser level (page type × traffic, then page type, then rest).
3. Within each stratum URLs are sorted by clicks and cut into blocks of
   `n_groups`; inside each block the group labels are shuffled (matched blocks).
   This gives far better balance than plain randomisation.
4. Each candidate (seed) is checked: SMD per covariate, correlation of the group
   series, parallel trends and an A/A placebo on the pre-period. The first
   candidate that passes everything wins; otherwise the best scoring one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

COVARIATES = {
    "log_clicks": "Klikken (log)",
    "log_impressions": "Vertoningen (log)",
    "ctr": "CTR",
    "position": "Gem. positie",
    "trend": "Trend (2e helft vs 1e)",
}


@dataclass
class GroupingConfig:
    group_names: list[str] = field(default_factory=lambda: ["controle", "test"])
    page_types: list[tuple[str, str]] = field(default_factory=list)  # (label, regex)
    traffic_bins: int = 4
    use_trend: bool = True
    min_clicks: float = 1.0
    min_impressions: float = 0.0
    max_url_share: float = 0.02  # URLs above this share of total clicks are excluded
    seed: int = 42
    max_attempts: int = 50
    smd_max: float = 0.10
    corr_min: float = 0.90
    trend_p_min: float = 0.20
    placebo_max: float = 0.03  # max |relative placebo effect|
    planned_post_periods: int = 42  # for the indicative MDE

    @property
    def n_groups(self) -> int:
        return len(self.group_names)


@dataclass
class GroupingResult:
    assignments: pd.DataFrame
    excluded: pd.DataFrame
    balance: pd.DataFrame
    checks: pd.DataFrame
    series: pd.DataFrame
    passed: bool
    attempt: int
    seed_used: int
    notes: list[str] = field(default_factory=list)


# --- Features -------------------------------------------------------------------
def page_type_of(urls: pd.Series, page_types: list[tuple[str, str]]) -> pd.Series:
    if not page_types:
        return pd.Series("alle", index=urls.index)
    out = pd.Series("overig", index=urls.index)
    assigned = pd.Series(False, index=urls.index)
    for label, pattern in page_types:
        hit = urls.str.contains(pattern, regex=True, na=False) & ~assigned
        out[hit] = label
        assigned |= hit
    return out


def url_features(long: pd.DataFrame, page_types: list[tuple[str, str]]) -> pd.DataFrame:
    dates = np.sort(long["date"].unique())
    mid = dates[len(dates) // 2]
    g = long.groupby("url")
    f = pd.DataFrame({
        "clicks": g["clicks"].sum(),
        "impressions": g["impressions"].sum(),
        "sum_position": g["sum_position"].sum(min_count=1),
    })
    first = long[long["date"] < mid].groupby("url")["clicks"].sum()
    second = long[long["date"] >= mid].groupby("url")["clicks"].sum()
    f["trend"] = np.log((second.reindex(f.index).fillna(0) + 1) / (first.reindex(f.index).fillna(0) + 1))
    f["ctr"] = np.where(f["impressions"] > 0, f["clicks"] / f["impressions"], np.nan)
    f["position"] = np.where(f["impressions"] > 0, f["sum_position"] / f["impressions"] + 1, np.nan)
    f["log_clicks"] = np.log1p(f["clicks"])
    f["log_impressions"] = np.log1p(f["impressions"])
    f = f.reset_index()
    f["page_type"] = page_type_of(f["url"], page_types)
    return f


# --- Strata ---------------------------------------------------------------------
def _qbin(x: pd.Series, q: int) -> pd.Series:
    q = max(1, min(q, x.nunique()))
    if q == 1:
        return pd.Series(0, index=x.index)
    return pd.qcut(x.rank(method="first"), q, labels=False)


def build_strata(f: pd.DataFrame, cfg: GroupingConfig) -> pd.Series:
    k = cfg.n_groups
    traffic = f.groupby("page_type")["log_clicks"].transform(
        lambda s: _qbin(s, min(cfg.traffic_bins, max(1, len(s) // (2 * k))))
    ).astype(int)
    trend = (
        f.groupby(["page_type", traffic])["trend"].transform(lambda s: _qbin(s, 3 if len(s) >= 6 * k else 1)).astype(int)
        if cfg.use_trend else pd.Series(0, index=f.index)
    )
    trend_names = {0: "dalend", 1: "stabiel", 2: "stijgend"}
    lvl3 = f["page_type"] + " | verkeer " + (traffic + 1).astype(str) + " | " + trend.map(trend_names).fillna("")
    lvl2 = f["page_type"] + " | verkeer " + (traffic + 1).astype(str)
    lvl1 = f["page_type"]
    stratum = lvl3.copy()
    for finer, coarser in ((lvl3, lvl2), (lvl2, lvl1)):
        small = stratum.map(stratum.value_counts()) < 2 * k
        stratum[small] = coarser[small]
    small = stratum.map(stratum.value_counts()) < k
    stratum[small] = "rest"
    return stratum


# --- Assignment -----------------------------------------------------------------
def assign(f: pd.DataFrame, strata: pd.Series, names: list[str], rng: np.random.Generator) -> pd.Series:
    k = len(names)
    out = pd.Series(index=f.index, dtype=object)
    counts = dict.fromkeys(names, 0)
    leftovers = []
    for _, idx in f.groupby(strata).groups.items():
        order = f.loc[idx].sort_values("clicks", ascending=False).index.to_list()
        full = len(order) - len(order) % k
        for b in range(0, full, k):
            labels = rng.permutation(names)
            for url_idx, lab in zip(order[b:b + k], labels):
                out[url_idx] = lab
                counts[lab] += 1
        leftovers.extend(order[full:])
    for url_idx in rng.permutation(leftovers):
        low = min(counts.values())
        lab = rng.choice([n for n, c in counts.items() if c == low])
        out[url_idx] = lab
        counts[lab] += 1
    return out


# --- Checks ---------------------------------------------------------------------
def _smd(a: pd.Series, b: pd.Series) -> float:
    a, b = a.dropna(), b.dropna()
    sd = np.sqrt((a.var() + b.var()) / 2)
    return 0.0 if sd == 0 or not np.isfinite(sd) else float((a.mean() - b.mean()) / sd)


def balance_table(f: pd.DataFrame, cfg: GroupingConfig) -> pd.DataFrame:
    ctrl = f[f["groep"] == cfg.group_names[0]]
    rows = []
    for test in cfg.group_names[1:]:
        tg = f[f["groep"] == test]
        for col, label in COVARIATES.items():
            smd = _smd(tg[col], ctrl[col])
            rows.append({
                "groep": test, "kenmerk": label,
                "controle": ctrl[col].mean(), "test": tg[col].mean(),
                "smd": smd, "ok": abs(smd) <= cfg.smd_max,
            })
    return pd.DataFrame(rows)


def series_checks(series: pd.DataFrame, cfg: GroupingConfig, season: int) -> pd.DataFrame:
    ctrl = series[cfg.group_names[0]]
    n = len(series)
    rows = []
    for test in cfg.group_names[1:]:
        y = series[test]
        corr = float(np.corrcoef(y, ctrl)[0, 1]) if n > 2 else np.nan

        ratio = np.log((y + 1) / (ctrl + 1))
        t = np.arange(n)
        # Trend in the test/control ratio. Daily ratios are autocorrelated, so a
        # plain regression p-value is too small; HAC (Newey-West) errors fix that.
        import statsmodels.api as sm
        lags = 7 if season == 7 else 2
        ols = sm.OLS(ratio.to_numpy(), sm.add_constant(t)).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
        slope, trend_p = float(ols.params[1]), float(ols.pvalues[1])
        slope_per_week = slope * (7 if season == 7 else 1)

        # A/A placebo: fit test ~ control (log) on the first 70%, predict the rest.
        # Back-transforming from logs needs a smearing factor (Duan), or the
        # prediction is biased low for noisy groups.
        cut = max(int(n * 0.7), 3)
        lx, ly = np.log1p(ctrl.values), np.log1p(y.values)
        b = np.polyfit(lx[:cut], ly[:cut], 1)
        smear = float(np.mean(np.exp(ly[:cut] - np.polyval(b, lx[:cut]))))
        pred = np.exp(np.polyval(b, lx[cut:])) * smear - 1
        placebo = float(y.values[cut:].sum() / max(pred.sum(), 1e-9) - 1) if n - cut > 0 else np.nan

        # Indicative MDE: noise of the detrended log ratio, corrected for autocorrelation.
        resid = ratio - (ols.params[0] + slope * t)
        sd = float(np.std(resid, ddof=2)) if n > 3 else np.nan
        rho = float(pd.Series(resid).autocorr(1)) if n > 3 else 0.0
        rho = 0.0 if not np.isfinite(rho) else min(max(rho, 0.0), 0.9)
        sd_eff = sd * np.sqrt((1 + rho) / (1 - rho))
        m = cfg.planned_post_periods
        mde = float(np.expm1((1.96 + 0.84) * sd_eff * np.sqrt(1 / max(m, 1) + 1 / n)))

        rows.append({
            "groep": test,
            "correlatie": corr,
            "trendverschil_per_week": float(np.expm1(slope_per_week)),
            "trend_p": trend_p,
            "placebo_effect": placebo,
            "mde_indicatief": mde,
            "ok_correlatie": corr >= cfg.corr_min,
            "ok_trend": trend_p >= cfg.trend_p_min,
            "ok_placebo": abs(placebo) <= cfg.placebo_max,
        })
    return pd.DataFrame(rows)


def _score(balance: pd.DataFrame, checks: pd.DataFrame, cfg: GroupingConfig) -> float:
    s = (balance["smd"].abs() / cfg.smd_max).clip(lower=1).sum() - len(balance)
    s += ((cfg.corr_min - checks["correlatie"]).clip(lower=0) * 20).sum()
    s += ((cfg.trend_p_min - checks["trend_p"]).clip(lower=0) * 10).sum()
    s += ((checks["placebo_effect"].abs() - cfg.placebo_max).clip(lower=0) * 50).sum()
    return float(s)


# --- Orchestration ----------------------------------------------------------------
def make_groups(long: pd.DataFrame, cfg: GroupingConfig, season: int = 7) -> GroupingResult:
    if cfg.n_groups < 2:
        raise ValueError("Minimaal twee groepen nodig (controle + test).")
    for _, pat in cfg.page_types:
        re.compile(pat)

    notes: list[str] = []
    f = url_features(long, cfg.page_types)

    total = f["clicks"].sum()
    reasons = pd.Series("", index=f.index)
    reasons[f["clicks"] < cfg.min_clicks] = "te weinig klikken"
    reasons[(reasons == "") & (f["impressions"] < cfg.min_impressions)] = "te weinig vertoningen"
    if cfg.max_url_share > 0 and total > 0:
        reasons[(reasons == "") & (f["clicks"] / total > cfg.max_url_share)] = "te groot aandeel (uitschieter)"
    excluded = f[reasons != ""].assign(reden=reasons[reasons != ""])
    f = f[reasons == ""].reset_index(drop=True)

    if len(f) < 10 * cfg.n_groups:
        raise ValueError(
            f"Te weinig url's over ({len(f)}) voor {cfg.n_groups} groepen. "
            "Verruim de patronen of verlaag de drempels."
        )

    strata = build_strata(f, cfg)
    if (strata == "rest").any():
        notes.append(f"{int((strata == 'rest').sum())} url's in te kleine strata zitten samen in 'rest'.")

    best = None
    for attempt in range(cfg.max_attempts):
        seed = cfg.seed + attempt
        rng = np.random.default_rng(seed)
        cand = f.assign(groep=assign(f, strata, cfg.group_names, rng), stratum=strata)
        bal = balance_table(cand, cfg)
        ser = (
            long.merge(cand[["url", "groep"]], on="url")
            .pivot_table(index="date", columns="groep", values="clicks", aggfunc="sum")
            .reindex(columns=cfg.group_names).fillna(0).sort_index()
        )
        chk = series_checks(ser, cfg, season)
        ok = bal["ok"].all() and chk[["ok_correlatie", "ok_trend", "ok_placebo"]].all().all()
        score = _score(bal, chk, cfg)
        if best is None or score < best[0]:
            best = (score, attempt, seed, cand, bal, chk, ser, ok)
        if ok:
            break

    _, attempt, seed, cand, bal, chk, ser, ok = best
    if not ok:
        notes.append(
            f"Geen indeling gevonden die alle toetsen haalt in {cfg.max_attempts} pogingen; "
            "de best scorende is gekozen. Kijk welke toets faalt."
        )
        if (chk["correlatie"] < cfg.corr_min).any():
            notes.append("Lage correlatie wijst meestal op te weinig verkeer per groep. Verruim de scope of meet per week.")

    assignments = cand[[
        "url", "groep", "stratum", "page_type", "clicks", "impressions", "ctr", "position", "trend"
    ]].sort_values(["groep", "stratum", "clicks"], ascending=[True, True, False]).reset_index(drop=True)

    return GroupingResult(
        assignments=assignments, excluded=excluded, balance=bal, checks=chk, series=ser,
        passed=bool(ok), attempt=attempt + 1, seed_used=seed, notes=notes,
    )
