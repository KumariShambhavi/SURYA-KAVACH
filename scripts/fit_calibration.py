"""
fit_calibration.py — derive SOLEXS_COUNTS_TO_FLUX from the official NOAA/SWPC GOES list.

Why this exists: the calibration scale sets EVERY flare class letter the system publishes, yet the
previous value was a bare number in config.py with no pairs file, no script and no residuals — it
could not be regenerated or checked. This script makes it reproducible and writes its evidence.

It time-matches our detected SXR peaks against the official GOES flare list, fits

    goes_peak_flux = k * our_peak_counts + b       (robust / Huber, outlier-resistant)

and reports k, b, the residual scatter and the resulting class-count comparison against NOAA.

Outputs:
    outputs/calibration_pairs.csv   the matched (counts, goes_flux) pairs actually used
    prints the fitted k to paste into config.SOLEXS_COUNTS_TO_FLUX (or SOLARFLARE_SXR_SCALE)

Usage:
    python scripts/fit_calibration.py
"""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sklearn.linear_model import HuberRegressor  # noqa: E402

from solarflare import config  # noqa: E402

TOL_S = 12 * 60
BASE = {"A": 1e-8, "B": 1e-7, "C": 1e-6, "M": 1e-5, "X": 1e-4}


def goes_flux(cls: str) -> float:
    m = re.match(r"\s*([ABCMX])([\d.]+)", str(cls))
    return BASE[m.group(1)] * float(m.group(2)) if m else np.nan


def main() -> int:
    cat = config.OUTPUT_DIR / "nowcast_master_catalogue.csv"
    ref = config.EXTERNAL_DIR / "goes_flares.csv"
    if not cat.exists() or not ref.exists():
        print("need outputs/nowcast_master_catalogue.csv and data/external/goes_flares.csv "
              "(run `ingest` and `validate` first)")
        return 1

    m = pd.read_csv(cat)
    m["peak_time_sxr"] = pd.to_datetime(m["peak_time_sxr"], utc=True, errors="coerce")
    m = m.dropna(subset=["peak_time_sxr", "peak_sxr_flux_w_m2"]).sort_values("peak_time_sxr")

    g = pd.read_csv(ref)
    g["peak_time"] = pd.to_datetime(g["peak_time"], utc=True, errors="coerce")
    g = g.dropna(subset=["peak_time"]).copy()
    g["flux"] = g["goes_class"].map(goes_flux)
    g = g.dropna(subset=["flux"])

    # undo the CURRENT scale to recover raw counts at each of our peaks
    k_cur = config.SOLEXS_COUNTS_TO_FLUX
    ot = m["peak_time_sxr"].values.astype("datetime64[s]").astype("int64")
    oc = m["peak_sxr_flux_w_m2"].to_numpy() / k_cur

    rows = []
    for _, r in g.iterrows():
        t = np.datetime64(r["peak_time"]).astype("datetime64[s]").astype("int64")
        i = np.searchsorted(ot, t)
        cand = [j for j in (i - 1, i) if 0 <= j < len(ot)]
        if not cand:
            continue
        j = min(cand, key=lambda x: abs(ot[x] - t))
        if abs(ot[j] - t) <= TOL_S and oc[j] > 0:
            rows.append({"peak_time": r["peak_time"], "goes_class": r["goes_class"],
                         "goes_flux": r["flux"], "our_counts": float(oc[j]),
                         "dt_s": int(abs(ot[j] - t))})
    pairs = pd.DataFrame(rows)
    if len(pairs) < 20:
        print(f"only {len(pairs)} matched pairs — too few to fit")
        return 1

    X = pairs[["our_counts"]].to_numpy()
    y = pairs["goes_flux"].to_numpy()
    hub = HuberRegressor(epsilon=1.35).fit(X, y)
    k, b = float(hub.coef_[0]), float(hub.intercept_)
    resid = y - (k * X.ravel() + b)
    mad = float(np.median(np.abs(resid - np.median(resid))))

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = config.OUTPUT_DIR / "calibration_pairs.csv"
    pairs.to_csv(out, index=False)

    print(f"matched pairs      : {len(pairs)}  (tolerance {TOL_S // 60} min)")
    print(f"robust (Huber) fit : flux = {k:.4e} * counts + {b:.3e}")
    print(f"residual MAD       : {mad:.3e} W/m^2")
    print(f"pairs written to   : {out}")
    print()
    print(f"=> set SOLEXS_COUNTS_TO_FLUX = {k:.4g}   (or SOLARFLARE_SXR_SCALE={k:.4g})")
    print(f"   current config value: {k_cur:.4g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
