"""CausalImpact analysis with two engines and one result format.

Engines
-------
- "tfcausalimpact": Google's model (TensorFlow Probability port), run in a child
  process with a timeout. Falls back to statsmodels on error or timeout.
- "statsmodels": structural time series (local level + weekly seasonality +
  control series as regressors), counterfactual via simulated paths. Fast and
  stable; parameter uncertainty is not included, so intervals are slightly
  narrower than tfcausalimpact's.

Both return an `ImpactResult` with a frame (observed, pred, bands, pointwise and
cumulative effects) and the same KPI keys, so plotting and the UI do not care
which engine ran.
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from .data_io import nl, nl_pct  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ImpactResult:
    frame: pd.DataFrame
    kpis: dict
    engine: str
    pre_fit: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --- Period handling ------------------------------------------------------------
def build_model_frame(
    data: pd.DataFrame, intervention: str, post_start: str | None = None
) -> tuple[pd.DataFrame, int, list[str]]:
    """Drop the roll-out window (intervention ≤ date < post_start) and return the
    frame, the number of pre rows and notes."""
    notes = []
    data = data.sort_index().astype(float)
    t0 = pd.Timestamp(intervention)
    t1 = pd.Timestamp(post_start) if post_start else t0
    if t1 < t0:
        raise ValueError("Start van de meetperiode ligt vóór de livegang.")
    pre = data[data.index < t0]
    post = data[data.index >= t1]
    need = min_pre_points(data.index)
    if len(pre) < need:
        unit = "dagen" if _is_daily(data.index) else "weken"
        raise ValueError(f"Te weinig pre-periode ({len(pre)} {unit}). Minimaal {need} {unit} nodig.")
    if len(post) < 3:
        raise ValueError("Te weinig data na de livegang (minimaal 3 punten).")
    gap = len(data) - len(pre) - len(post)
    if gap:
        notes.append(f"{gap} periode(s) in het uitrolvenster zijn buiten de analyse gelaten.")
        if gap % 7 and _is_daily(data.index):
            notes.append("Het uitrolvenster is geen veelvoud van 7 dagen; het weekpatroon schuift daardoor iets op.")
    return pd.concat([pre, post]), len(pre), notes


def min_pre_points(idx: pd.Index) -> int:
    """Minimum pre-period length: 14 days, or 8 weeks for weekly data."""
    return 14 if _is_daily(idx) else 8


def _is_daily(idx: pd.Index) -> bool:
    return len(idx) > 2 and pd.Series(idx).diff().dt.days.median() < 2


def season_length(idx: pd.Index) -> int | None:
    return 7 if _is_daily(idx) else None


# --- KPIs -----------------------------------------------------------------------
def _kpis_from_sims(y_post: np.ndarray, sims: np.ndarray, alpha: float) -> dict:
    """sims: (n_post, R) simulated counterfactual paths."""
    lo, hi = 100 * alpha / 2, 100 * (1 - alpha / 2)
    pred_sum = sims.sum(axis=0)
    eff_sum = y_post.sum() - pred_sum
    n = len(y_post)
    rel = eff_sum / np.where(pred_sum == 0, np.nan, pred_sum)
    # Two-sided: twice the smaller tail. "Significant" (p < alpha) then means
    # exactly that the (1 - alpha) interval excludes zero. A one-sided tail
    # against a two-sided alpha doubled the false positives (measured: 20% at a
    # nominal 10%).
    p_tail = min((pred_sum >= y_post.sum()).mean(), (pred_sum <= y_post.sum()).mean())
    p_value = float(min(1.0, max(2 * p_tail, 1 / (sims.shape[1] + 1))))
    pred_mean_sum = float(np.mean(pred_sum))
    return {
        "actual_avg": float(y_post.mean()),
        "predicted_avg": pred_mean_sum / n,
        "abs_effect_avg": float(np.mean(eff_sum) / n),
        "abs_effect_avg_lower": float(np.percentile(eff_sum, lo) / n),
        "abs_effect_avg_upper": float(np.percentile(eff_sum, hi) / n),
        "actual_cum": float(y_post.sum()),
        "predicted_cum": pred_mean_sum,
        "abs_effect_cum": float(np.mean(eff_sum)),
        "abs_effect_cum_lower": float(np.percentile(eff_sum, lo)),
        "abs_effect_cum_upper": float(np.percentile(eff_sum, hi)),
        "rel_effect": float(np.nanmean(rel)),
        "rel_effect_lower": float(np.nanpercentile(rel, lo)),
        "rel_effect_upper": float(np.nanpercentile(rel, hi)),
        "p_value": p_value,
        "prob_causal": 1 - p_value,
        "significant": p_value < alpha,
        "alpha": alpha,
    }


# --- Engine: statsmodels --------------------------------------------------------
def _fit_and_simulate(y_pre: np.ndarray, X_pre: np.ndarray, X_future: np.ndarray, reps: int):
    """Fit the structural model on the pre-period and simulate counterfactual paths.

    Returns (fitted results, sims of shape (len(X_future), reps) in original
    units, scale). Shared by the analysis and the duration advice so both use
    exactly the same model.
    """
    from statsmodels.tsa.statespace.structural import UnobservedComponents

    scale = max(float(np.mean(np.abs(y_pre))), 1e-9)
    xmean = np.abs(X_pre).mean(axis=0) if X_pre.shape[1] else np.ones(0)
    xmean = np.where(xmean == 0, 1, xmean)
    ys, xs_pre, xs_fut = y_pre / scale, X_pre / xmean, X_future / xmean

    # Mirrors CausalImpact's defaults: local level with a small, fixed level
    # variance (prior sd 0.01 × sd(y)) and no own seasonality, because the
    # control series already carry the weekly pattern. A free level variance or an
    # extra seasonal component lets the level absorb the effect (tested: biased).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mod = UnobservedComponents(ys, level="local level", exog=xs_pre if X_pre.shape[1] else None)
        level_sd = 0.01 * float(np.std(ys))
        res = mod.fit_constrained({"sigma2.level": level_sd ** 2}, disp=False, maxiter=1000)
        np.random.seed(0)  # reproducible paths; passing random_state clashes with some pandas versions
        sims = res.simulate(
            nsimulations=len(X_future), repetitions=reps, anchor="end",
            exog=xs_fut if X_pre.shape[1] else None,
        )
    return res, np.asarray(sims).reshape(len(X_future), -1) * scale, scale


def effective_horizon(horizon: int, n_pre: int) -> int:
    """Backtest horizon: the test length, but at most a quarter of the pre-period."""
    return int(min(horizon, n_pre // 4))


def calibration_factor(y_pre: np.ndarray, X_pre: np.ndarray, horizon: int, n_backtests: int = 8,
                       min_train: int = 14) -> float:
    """How much wider the intervals must be, measured on the pre-period itself.

    The model's intervals assume the future is as calm as the past noise. When
    groups slowly drift apart (common in real SEO data), out-of-sample errors are
    larger than the model thinks. We backtest: start a fake test at several
    points in the pre-period, forecast `horizon` periods, and compare the real
    error of the total with the simulated spread. The factor is the RMS of those
    standardised errors, never below 1 (we only widen) and capped at 4. On the
    demo data it is about 1.06; it is a safety net for drifting real data.
    """
    n = len(y_pre)
    h = effective_horizon(horizon, n)
    first = max(int(n * 0.5), min_train)
    last = n - h
    if h < 3 or last <= first:
        return 1.0
    origins = np.unique(np.linspace(first, last, n_backtests).astype(int))
    z = []
    for t in origins:
        try:
            _, sims, _ = _fit_and_simulate(y_pre[:t], X_pre[:t], X_pre[t:t + h], reps=300)
        except Exception:  # noqa: BLE001 — a failed backtest just does not count
            continue
        totals = sims.sum(axis=0)
        sd = totals.std()
        if sd > 0:
            z.append((y_pre[t:t + h].sum() - totals.mean()) / sd)
    if len(z) < 3:
        return 1.0
    return float(np.clip(np.sqrt(np.mean(np.square(z))), 1.0, 4.0))


def _widen(sims: np.ndarray, factor: float) -> np.ndarray:
    """Scale every simulated path's deviation from the mean path by `factor`."""
    if factor == 1.0:
        return sims
    mean = sims.mean(axis=1, keepdims=True)
    return mean + factor * (sims - mean)


def _run_statsmodels(data: pd.DataFrame, n_pre: int, alpha: float, reps: int = 1000) -> ImpactResult:
    y = data.iloc[:, 0].to_numpy(float)
    X = data.iloc[:, 1:].to_numpy(float)
    season = season_length(data.index)
    res, sims, scale = _fit_and_simulate(y[:n_pre], X[:n_pre], X[n_pre:], reps)
    factor = calibration_factor(y[:n_pre], X[:n_pre], len(y) - n_pre, min_train=min_pre_points(data.index))
    sims = _widen(sims, factor)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pre_pred = res.get_prediction()

    lo, hi = 100 * alpha / 2, 100 * (1 - alpha / 2)
    pre_mean = pre_pred.predicted_mean * scale
    ci = pre_pred.conf_int(alpha=alpha) * scale
    burn = min(season or 7, n_pre // 4)  # diffuse start: hide the first points
    pre_mean[:burn] = np.nan
    ci[:burn] = np.nan

    post_mean = sims.mean(axis=1)
    frame = pd.DataFrame(index=data.index)
    frame["observed"] = y
    frame["pred"] = np.r_[pre_mean, post_mean]
    frame["pred_lower"] = np.r_[ci[:, 0], np.percentile(sims, lo, axis=1)]
    frame["pred_upper"] = np.r_[ci[:, 1], np.percentile(sims, hi, axis=1)]
    eff_sims = y[n_pre:, None] - sims
    frame["effect"] = frame["observed"] - frame["pred"]
    frame["effect_lower"] = frame["observed"] - frame["pred_upper"]
    frame["effect_upper"] = frame["observed"] - frame["pred_lower"]
    cum = np.cumsum(eff_sims, axis=0)
    frame["cum"] = np.r_[np.full(n_pre, np.nan), cum.mean(axis=1)]
    frame["cum_lower"] = np.r_[np.full(n_pre, np.nan), np.percentile(cum, lo, axis=1)]
    frame["cum_upper"] = np.r_[np.full(n_pre, np.nan), np.percentile(cum, hi, axis=1)]
    kpis = _kpis_from_sims(y[n_pre:], sims, alpha)
    kpis["calibration"] = factor
    return ImpactResult(frame=frame, kpis=kpis, engine="statsmodels")


# --- Engine: tfcausalimpact (child process) -------------------------------------
_COLS = {
    "pred": ["complete_preds_means", "preds", "point_pred"],
    "pred_lower": ["complete_preds_lower", "preds_lower", "point_pred_lower"],
    "pred_upper": ["complete_preds_upper", "preds_upper", "point_pred_upper"],
    "effect": ["point_effects_means", "point_effects", "point_effect"],
    "effect_lower": ["point_effects_lower", "point_effect_lower"],
    "effect_upper": ["point_effects_upper", "point_effect_upper"],
    "cum": ["post_cum_effects_means", "post_cum_effects", "cum_effect"],
    "cum_lower": ["post_cum_effects_lower", "cum_effect_lower"],
    "cum_upper": ["post_cum_effects_upper", "cum_effect_upper"],
}


def tfci_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("causalimpact") is not None


def _run_tfci(data: pd.DataFrame, n_pre: int, alpha: float, timeout: int) -> ImpactResult:
    ints = data.reset_index(drop=True)
    job = {
        "data": ints,
        "pre_period": [0, n_pre - 1],
        "post_period": [n_pre, len(ints) - 1],
        "alpha": alpha,
    }
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "in.pkl"), Path(tmp, "out.pkl")
        with open(src, "wb") as fh:
            pickle.dump(job, fh)
        env = {**os.environ, "TF_CPP_MIN_LOG_LEVEL": "3", "PYTHONWARNINGS": "ignore"}
        proc = subprocess.run(
            [sys.executable, "-m", "core._tfci_worker", str(src), str(dst)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0 or not dst.exists():
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError("tfcausalimpact faalde: " + " ".join(tail))
        with open(dst, "rb") as fh:
            out = pickle.load(fh)

    inf = out["inferences"].reset_index(drop=True)
    frame = pd.DataFrame(index=data.index)
    frame["observed"] = data.iloc[:, 0].to_numpy(float)
    for key, cands in _COLS.items():
        col = next((c for c in cands if c in inf.columns), None)
        frame[key] = inf[col].to_numpy(float) if col else np.nan
    if frame["effect"].isna().all():
        frame["effect"] = frame["observed"] - frame["pred"]
        frame["effect_lower"] = frame["observed"] - frame["pred_upper"]
        frame["effect_upper"] = frame["observed"] - frame["pred_lower"]
    frame.loc[frame.index[:n_pre], ["cum", "cum_lower", "cum_upper"]] = np.nan

    sd = out["summary_data"]
    v = lambda r, c: float(sd.loc[r, c])  # noqa: E731
    p = min(1.0, 2 * out["p_value"])  # tfcausalimpact reports one tail; make it two-sided
    kpis = {
        "actual_avg": v("actual", "average"),
        "predicted_avg": v("predicted", "average"),
        "abs_effect_avg": v("abs_effect", "average"),
        "abs_effect_avg_lower": v("abs_effect_lower", "average"),
        "abs_effect_avg_upper": v("abs_effect_upper", "average"),
        "actual_cum": v("actual", "cumulative"),
        "predicted_cum": v("predicted", "cumulative"),
        "abs_effect_cum": v("abs_effect", "cumulative"),
        "abs_effect_cum_lower": v("abs_effect_lower", "cumulative"),
        "abs_effect_cum_upper": v("abs_effect_upper", "cumulative"),
        "rel_effect": v("rel_effect", "average"),
        "rel_effect_lower": v("rel_effect_lower", "average"),
        "rel_effect_upper": v("rel_effect_upper", "average"),
        "p_value": p,
        "prob_causal": 1 - p,
        "significant": bool(np.isfinite(p) and p < alpha),
        "alpha": alpha,
    }
    return ImpactResult(frame=frame, kpis=kpis, engine="tfcausalimpact")


# --- Public API -----------------------------------------------------------------
def pre_fit_quality(frame: pd.DataFrame, n_pre: int) -> dict:
    pre = frame.iloc[:n_pre].dropna(subset=["pred"])
    if pre.empty:
        return {}
    obs, pred = pre["observed"], pre["pred"]
    mape = float((np.abs(obs - pred) / obs.replace(0, np.nan)).mean())
    corr = float(np.corrcoef(obs, pred)[0, 1]) if len(pre) > 2 else np.nan
    return {"mape": mape, "corr": corr}


def run_impact(
    data: pd.DataFrame,
    intervention: str,
    post_start: str | None = None,
    engine: str = "statsmodels",
    alpha: float = 0.05,
    timeout: int = 300,
) -> ImpactResult:
    """`data`: DatetimeIndex, treated series first, control series after."""
    if data.shape[1] < 2:
        raise ValueError("Minimaal één controlereeks nodig.")
    model_df, n_pre, notes = build_model_frame(data, intervention, post_start)

    if engine == "tfcausalimpact":
        if not tfci_available():
            notes.append("tfcausalimpact is niet geïnstalleerd; statsmodels gebruikt.")
            result = _run_statsmodels(model_df, n_pre, alpha)
        else:
            try:
                result = _run_tfci(model_df, n_pre, alpha, timeout)
            except subprocess.TimeoutExpired:
                notes.append(f"tfcausalimpact reageerde niet binnen {timeout} s (bekende deadlock); statsmodels gebruikt.")
                result = _run_statsmodels(model_df, n_pre, alpha)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{exc} Statsmodels gebruikt.")
                result = _run_statsmodels(model_df, n_pre, alpha)
    else:
        result = _run_statsmodels(model_df, n_pre, alpha)

    result.pre_fit = pre_fit_quality(result.frame, n_pre)
    result.warnings = notes + result.warnings
    result.kpis["n_pre"] = n_pre
    result.kpis["n_post"] = len(model_df) - n_pre
    result.kpis["intervention"] = str(pd.Timestamp(intervention).date())
    result.kpis["post_start"] = str(model_df.index[n_pre].date())
    result.kpis["engine"] = result.engine
    return result


def run_placebo(
    data: pd.DataFrame, intervention: str, engine: str = "statsmodels",
    alpha: float = 0.05, timeout: int = 300, share: float = 0.3,
) -> ImpactResult:
    """Fake intervention inside the pre-period. A sound setup finds nothing here."""
    pre = data[data.index < pd.Timestamp(intervention)]
    n_fake = max(7 if _is_daily(pre.index) else 3, int(len(pre) * share))
    if len(pre) - n_fake < min_pre_points(pre.index):
        raise ValueError("pre-periode te kort voor een placebotest.")
    fake = pre.index[len(pre) - n_fake]
    return run_impact(pre, str(fake.date()), engine=engine, alpha=alpha, timeout=timeout)


# --- Plot -----------------------------------------------------------------------
def plot_impact(result: ImpactResult, metric: str = "klikken", title: str | None = None) -> Figure:
    k = result.kpis
    t0 = pd.Timestamp(k["post_start"])
    t_live = pd.Timestamp(k["intervention"])
    fr = result.frame
    step = "D" if _is_daily(fr.index) else "7D"
    fr = fr.reindex(pd.date_range(fr.index.min(), fr.index.max(), freq=step))  # break lines in the roll-out gap
    idx = fr.index
    nl_fmt = FuncFormatter(lambda v, _: nl(v))

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    ink, accent, band, eff_c, cum_c = "#111827", "#2563eb", "#93c5fd", "#7c3aed", "#059669"

    ax = axes[0]
    ax.plot(idx, fr["observed"], color=ink, lw=1.5, label="Werkelijk (testgroep)")
    ax.plot(idx, fr["pred"], color=accent, lw=1.5, ls="--", label="Verwacht zonder ingreep")
    ax.fill_between(idx, fr["pred_lower"], fr["pred_upper"], color=band, alpha=0.4,
                    label=f"{int((1 - k['alpha']) * 100)}%-interval")
    ax.set_title("Werkelijk tegen verwacht", loc="left", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, frameon=False)

    ax = axes[1]
    ax.axhline(0, color="#9ca3af", lw=1)
    ax.plot(idx, fr["effect"], color=eff_c, lw=1.3)
    ax.fill_between(idx, fr["effect_lower"], fr["effect_upper"], color=eff_c, alpha=0.15)
    ax.set_title("Effect per periode", loc="left", fontsize=11, fontweight="bold")

    ax = axes[2]
    ax.axhline(0, color="#9ca3af", lw=1)
    ax.plot(idx, fr["cum"], color=cum_c, lw=1.6)
    ax.fill_between(idx, fr["cum_lower"], fr["cum_upper"], color="#6ee7b7", alpha=0.35)
    ax.set_title("Cumulatief effect", loc="left", fontsize=11, fontweight="bold")

    for a, lab in zip(axes, [metric, f"Δ {metric}", f"Σ Δ {metric}"]):
        if t0 > t_live:
            a.axvspan(t_live, t0, color="#e5e7eb", alpha=0.7, lw=0)
        a.axvline(t0, color="#dc2626", ls=":", lw=1.3)
        a.yaxis.set_major_formatter(nl_fmt)
        a.set_ylabel(lab)
        a.margins(x=0.01)
        a.spines[["top", "right"]].set_visible(False)

    verdict = "significant" if k["significant"] else "niet significant"
    banner = title or (
        f"Effect {nl_pct(k['rel_effect'])} "
        f"({nl_pct(k['rel_effect_lower'])} tot {nl_pct(k['rel_effect_upper'])})  ·  "
        f"cumulatief {nl(k['abs_effect_cum'], sign=True)} {metric}  ·  "
        f"p = {nl(k['p_value'], 3)}, {verdict}"
    )
    fig.suptitle(banner, fontsize=11, fontweight="bold", color=ink, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig
