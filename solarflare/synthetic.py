"""
synthetic.py — generate physically-plausible light curves for tests & validation.

Models a quiet-Sun background plus injected FRED-shaped soft X-ray flares, each
accompanied by an earlier hard X-ray pulse (the Neupert effect: HXR leads SXR).
Returns a canonical light curve + the ground-truth flare list, so detection and
leakage tests have a known answer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def generate(hours: float = 6.0, flares: list[dict] | None = None,
             seed: int = 7, gaps: bool = False) -> tuple[pd.DataFrame, list[dict]]:
    """Return (canonical_df, truth) with injected flares.

    Each flare dict: {offset_s, peak_flux, rise_s, decay_s, hxr_amp, hxr_lead_s}.
    Pass ``flares=[]`` for a QUIET SUN (no flares) — the detector must find nothing.
    Pass ``gaps=True`` to punch NaN holes (with quality=1) so gap/fill logic is exercised.

    NOISE IS POISSON, and that matters. The original generator used Gaussian noise with
    sigma/mu = 0.04, while real SoLEXS has a Poisson floor of ~1/sqrt(counts). That made the
    synthetic Sun ~20x quieter than the instrument, so the detector's quiet-Sun false-trigger
    behaviour was UNREACHABLE in every test — the suite passed while the real detector fired
    62% of the time. Photons are counted, so we count photons.
    """
    rng = np.random.default_rng(seed)
    n = int(hours * 3600)
    t0 = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
    time = pd.date_range(t0, periods=n, freq="1s")
    tsec = np.arange(n, dtype="float64")

    k = config.SOLEXS_COUNTS_TO_FLUX
    bg_counts = 60.0                             # realistic SoLEXS quiet-Sun count rate
    sxr = rng.poisson(bg_counts, n).astype("float64") * k    # Poisson shot noise, in flux units
    hxr = rng.poisson(50.0, n).astype("float64")             # ~50 c/s, Poisson

    if flares is None:
        # well-separated in time (each returns to background before the next) so the
        # detector treats them as distinct elevated episodes
        flares = [
            {"offset_s": 3600, "peak_flux": 3e-6, "rise_s": 240, "decay_s": 500,
             "hxr_amp": 400, "hxr_lead_s": 120},      # C  @ 01:00
            {"offset_s": 12600, "peak_flux": 2e-5, "rise_s": 300, "decay_s": 600,
             "hxr_amp": 1200, "hxr_lead_s": 180},     # M  @ 03:30
            {"offset_s": 19800, "peak_flux": 5e-6, "rise_s": 180, "decay_s": 500,
             "hxr_amp": 300, "hxr_lead_s": 90},       # C  @ 05:30
        ]

    truth = []
    for f in flares:
        tp = float(f["offset_s"])
        A = float(f["peak_flux"])
        rise, decay = float(f["rise_s"]), float(f["decay_s"])
        onset = tp - rise
        # soft X-ray FRED: linear-ish rise to A at tp, exponential decay after
        rising = (tsec >= onset) & (tsec <= tp)
        decaying = tsec > tp
        prof = np.zeros(n)
        prof[rising] = A * (tsec[rising] - onset) / rise
        prof[decaying] = A * np.exp(-(tsec[decaying] - tp) / decay)
        sxr += prof
        # hard X-ray Neupert pulse, peaks earlier (tp - lead)
        hp = tp - float(f["hxr_lead_s"])
        width = max(20.0, rise / 2)
        hxr += float(f["hxr_amp"]) * np.exp(-0.5 * ((tsec - hp) / width) ** 2)
        truth.append({
            "onset_time": t0 + pd.Timedelta(seconds=onset),
            "peak_time": t0 + pd.Timedelta(seconds=tp),
            "peak_flux": A,
        })

    sq = np.zeros(n, dtype="int8")
    hq = np.zeros(n, dtype="int8")
    if gaps:
        # real data has holes: 5 s, 60 s and a 3 h HXR outage. Without these, the ffill/bfill
        # paths are never executed by the tests.
        for lo, hi, ch in ((1000, 1005, "sxr"), (5000, 5060, "sxr"),
                           (2000, 2000 + 3 * 3600, "hxr")):
            hi = min(hi, n)
            if ch == "sxr":
                sxr[lo:hi] = np.nan
                sq[lo:hi] = 1
            else:
                hxr[lo:hi] = np.nan
                hq[lo:hi] = 1

    df = pd.DataFrame({
        config.TIME_COL: time,
        config.SXR_COL: sxr,
        config.HXR_COL: hxr,
        config.SXR_QUALITY_COL: sq,
        config.HXR_QUALITY_COL: hq,
    })
    return df, truth
