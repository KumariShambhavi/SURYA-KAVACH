"""
calibrate.py — fit the SoLEXS counts -> GOES-equivalent flux scale.

SoLEXS Level-1 is in counts/s, not W/m^2. ``SOLEXS_COUNTS_TO_FLUX`` (config) maps
counts to a GOES-equivalent soft X-ray flux so A-X classification is meaningful.
It is a FITTABLE parameter: match a handful of flares to the NOAA/GOES 1-8 A peak
flux at the same UTC and fit the scale here. Detection, ranking, lead time and the
forecast model all work in count space and are UNAFFECTED by this scale — it only
sets the absolute displayed flux / class letter.

Usage:
    from solarflare.calibrate import fit_counts_to_flux
    scale = fit_counts_to_flux([(solexs_counts, goes_flux), ...])   # >= 2 pairs
    # then set SOLARFLARE_SXR_SCALE=<scale> (env) or config.SOLEXS_COUNTS_TO_FLUX
"""
from __future__ import annotations

import numpy as np


def fit_counts_to_flux(pairs: list[tuple[float, float]]) -> float:
    """Robust log-space fit of flux = scale * counts from matched (counts, flux) pairs.

    Uses the median of flux/counts in log space (resistant to outliers). Returns the
    linear ``scale`` factor.
    """
    arr = np.array([(c, f) for c, f in pairs if c > 0 and f > 0], dtype="float64")
    if len(arr) < 2:
        raise ValueError("need at least 2 positive (counts, flux) pairs")
    counts, flux = arr[:, 0], arr[:, 1]
    # flux = scale * counts  ->  scale = median(flux / counts), computed in log space
    log_scale = np.median(np.log(flux) - np.log(counts))
    return float(np.exp(log_scale))
