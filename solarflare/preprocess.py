"""
preprocess.py — causal feature engineering on the canonical light curve.

CAUSALITY: every derived value at time t uses only trailing windows (no peeking
into the future), so features are valid for real-time nowcasting/forecasting.

Gap policy: rolling stats are computed on a gap-filled working copy (forward/back
fill) so a single NaN does not poison a whole window; but ``*_excess`` is forced
to 0 wherever the original sample was missing (quality == 1), so the detector can
never trigger on fabricated data inside a gap.

Adds columns:
  sxr_bg, sxr_smooth, sxr_excess, sxr_ratio
  hxr_bg, hxr_smooth, hxr_excess, hxr_noise
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def _roll(s: pd.Series, win: int, *, kind: str, q: float = 0.0):
    mp = max(1, win // 10)
    r = s.rolling(window=win, min_periods=mp)
    if kind == "mean":
        return r.mean()
    if kind == "std":
        return r.std()
    if kind == "quantile":
        return r.quantile(q)
    raise ValueError(kind)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with causal background/excess/ratio/noise columns."""
    out = df.copy()
    bg_win = config.BACKGROUND_WINDOW_MIN * 60      # samples (1 Hz grid)
    smooth_win = config.SMOOTH_WINDOW_S
    pct = config.BACKGROUND_PERCENTILE / 100.0

    for ch, raw_col, qcol in (
        ("sxr", config.SXR_COL, config.SXR_QUALITY_COL),
        ("hxr", config.HXR_COL, config.HXR_QUALITY_COL),
    ):
        # ffill only. NEVER bfill: back-filling a leading gap copies a FUTURE sample
        # into the past, which is data leakage (it changes features at t=0 based on
        # data at t=5) and it is reachable in the real data (hxr first valid index=6).
        work = out[raw_col].ffill()
        smooth = _roll(work, smooth_win, kind="mean")
        # The background MUST be estimated from the same SMOOTHED statistic used in the
        # numerator. Taking a low percentile of the RAW (Poisson-noisy) counts puts the
        # background ~1.6 sigma BELOW the mean, so ratio = smooth/bg is biased HIGH on a
        # quiet Sun and the detector triggers on photon noise alone (measured: the old
        # code sat above the 1.5 trigger 62% of the time on real SoLEXS data).
        bg = _roll(smooth, bg_win, kind="quantile", q=pct)
        excess = (smooth - bg).clip(lower=0)
        # never let a fabricated (missing) sample produce signal
        excess = excess.where(out[qcol].to_numpy() == 0, 0.0)

        out[f"{ch}_smooth"] = smooth.to_numpy()
        out[f"{ch}_bg"] = bg.to_numpy()
        out[f"{ch}_excess"] = excess.to_numpy()

        if ch == "sxr":
            denom = bg.replace(0, np.nan)
            ratio = (smooth / denom).fillna(1.0)
            out["sxr_ratio"] = ratio.to_numpy()          # kept for the UI / legacy only
            # Analytic Poisson shot noise of the SMOOTHED signal. counts ~ Poisson(mu), and
            # `smooth` is the mean of N samples, so std(counts) = sqrt(mu/N). In flux units
            # (flux = k*counts, so mu = bg/k):  sigma = sqrt(k * bg / N).
            # This is the detector's significance denominator — a physically-derived noise
            # floor, not a percentile of a noisy series.
            k = config.SOLEXS_COUNTS_TO_FLUX
            sigma = np.sqrt(np.clip(k * bg.to_numpy() / max(1, smooth_win), 0.0, None))
            out["sxr_noise"] = np.clip(sigma, 1e-12, None)
        else:
            # scatter of the RAW series (the smoothed series' std is ~sqrt(smooth_win)
            # smaller, which would collapse the noise floor and fire on everything).
            # No bfill: that would back-fill a FUTURE statistic into the past.
            noise = _roll(work, bg_win, kind="std")
            out["hxr_noise"] = noise.clip(lower=1e-9).to_numpy()

    return out
