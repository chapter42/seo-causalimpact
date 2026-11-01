"""Duration advice: how long must the test run to detect a given effect?

Uses the same structural model as the analysis (impact._fit_and_simulate):
fit on the whole pre-period, simulate counterfactual paths ahead, and read the
width of the interval around the cumulative total per horizon. The minimum
detectable effect (MDE) per horizon follows from that width and the chosen
power. Because it is the analysis model itself, the advice matches what step 2
will be able to show.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import norm

from .impact import _fit_and_simulate, _is_daily, _widen, calibration_factor, effective_horizon, min_pre_points

MIN_WEEKS = 3
MAX_WEEKS = 12


def mde_curve(
    test: pd.Series,
    control: pd.Series,
    alpha: float = 0.05,
    power: float = 0.80,
    max_weeks: int = MAX_WEEKS,
    reps: int = 2000,
) -> pd.DataFrame:
    """MDE per test length in whole weeks (2..max_weeks).

    Future control values are unknown, so the last four weeks of the control are
    repeated (that keeps the weekday pattern intact).
    """
    daily = _is_daily(test.index)
    per_week = 7 if daily else 1
    horizon = max_weeks * per_week
    y = test.to_numpy(float)
    X = control.to_numpy(float).reshape(-1, 1)
    tail = X[-4 * per_week:] if len(X) >= 4 * per_week else X
    X_future = np.resize(tail, (horizon, 1))

    _, sims, _ = _fit_and_simulate(y, X, X_future, reps)
    z_a, z_b = norm.ppf(1 - alpha / 2), norm.ppf(power)
    # Same widening as the analysis, per test length: the analysis calibrates on
    # min(test length, pre-period / 4), so we do exactly that per horizon.
    min_train = min_pre_points(test.index)
    factors: dict[int, float] = {}
    rows = []
    for weeks in range(2, max_weeks + 1):
        h = weeks * per_week
        eff = effective_horizon(h, len(y))
        if eff not in factors:
            factors[eff] = calibration_factor(y, X, h, min_train=min_train)
        totals = _widen(sims[:h], factors[eff]).sum(axis=0)
        lo, hi = np.percentile(totals, [100 * alpha / 2, 100 * (1 - alpha / 2)])
        half = (hi - lo) / 2 / max(totals.mean(), 1e-9)
        rows.append({"weken": weeks, "mde": float(half * (z_a + z_b) / z_a)})
    return pd.DataFrame(rows)


def advise(curve: pd.DataFrame, expected: float) -> dict:
    """Shortest whole number of weeks (≥ MIN_WEEKS) at which `expected` is detectable.

    The MDE does not keep falling with length: the forecast grows less certain
    further ahead, so there is a best length. That best point is returned too.
    """
    usable = curve[curve["weken"] >= MIN_WEEKS]
    best = usable.loc[usable["mde"].idxmin()]
    out = {"beste_weken": int(best["weken"]), "beste_mde": float(best["mde"])}
    ok = usable[usable["mde"] <= expected]
    if ok.empty:
        return {"haalbaar": False, "weken": None, **out}
    return {"haalbaar": True, "weken": int(ok["weken"].iloc[0]), "mde": float(ok["mde"].iloc[0]), **out}


def combined_curve(series: pd.DataFrame, group_names: list[str], **kw) -> pd.DataFrame:
    """MDE per week for every test group against the control; the advice takes
    the worst (largest) MDE so every test group is covered."""
    ctrl = series[group_names[0]]
    out = None
    for g in group_names[1:]:
        c = mde_curve(series[g], ctrl, **kw).rename(columns={"mde": g})
        out = c if out is None else out.merge(c, on="weken")
    out["mde"] = out[group_names[1:]].max(axis=1)
    return out


def weeks_to_text(weeks: int | None) -> str:
    if weeks is None:
        return "–"
    return f"{weeks} weken ({weeks * 7} dagen)"


def round_up_weeks(days: float) -> int:
    return max(MIN_WEEKS, math.ceil(days / 7))
