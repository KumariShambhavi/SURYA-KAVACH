"""
config.py — single source of truth for paths, the data schema, and every
tunable threshold used across the pipeline.

Design goal (from the brief): "when I get the actual ISSDC data, all I should
have to do is change the dataset." Every path, column name, unit and threshold
lives HERE; every other module imports from this file. Values can be overridden
at runtime via environment variables (prefix ``SOLARFLARE_``) so the same code
runs in dev, Docker, and CI without edits.
"""
from __future__ import annotations

import os
from pathlib import Path

# Load .env BEFORE any os.getenv() below. Without this, `.env.example` was a lie: operators set
# SOLARFLARE_API_KEY / SMTP creds / SOLARFLARE_SXR_SCALE in a .env file and NOTHING read them —
# auth stayed off while they believed it was on. python-dotenv is optional; if it is not
# installed we fall back to real environment variables only.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # pragma: no cover - optional dependency
    pass

# --------------------------------------------------------------------------
# 1. PATHS
# --------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent

# DATA_DIR may be relocated (recommended: outside OneDrive to avoid sync stalls)
# via SOLARFLARE_DATA_DIR. Defaults to <project>/data.
DATA_DIR = Path(os.getenv("SOLARFLARE_DATA_DIR", ROOT_DIR / "data")).resolve()
RAW_DIR = DATA_DIR / "raw"
SOLEXS_RAW_DIR = RAW_DIR / "solexs"      # converted SoLEXS CSVs (ISOT, COUNTS)
HEL1OS_RAW_DIR = RAW_DIR / "hel1os"      # converted HEL1OS CSVs (ISOT, CTR)
INCOMING_DIR = DATA_DIR / "incoming"     # drop raw PRADAN ZIPs here (optional)
PROCESSED_DIR = DATA_DIR / "processed"
WINDOW_CACHE_DIR = PROCESSED_DIR / "window_cache"
EXTERNAL_DIR = DATA_DIR / "external"     # optional external/validation catalogues
OUTPUT_DIR = ROOT_DIR / "outputs"

# Default place to look for raw PRADAN .zip downloads: data/incoming if it has
# zips, else the project root (where the user currently keeps them).
def default_incoming_dir() -> Path:
    if INCOMING_DIR.exists() and any(INCOMING_DIR.glob("*.zip")):
        return INCOMING_DIR
    return ROOT_DIR


DB_PATH = Path(os.getenv("SOLARFLARE_DB_PATH", DATA_DIR / "flare_ops.db"))

PROCESSED_LIGHT_CURVE = PROCESSED_DIR / "light_curve_standardized.parquet"


def ensure_dirs() -> None:
    """Create the data/output directory tree. Call before writing artifacts."""
    for d in (SOLEXS_RAW_DIR, HEL1OS_RAW_DIR, INCOMING_DIR, PROCESSED_DIR,
              WINDOW_CACHE_DIR, EXTERNAL_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# 2. CANONICAL SCHEMA
# --------------------------------------------------------------------------
TIME_COL = "time"
SXR_COL = "sxr_flux"          # soft X-ray, W/m^2 (GOES-equivalent, scaled from counts)
HXR_COL = "hxr_flux"          # hard X-ray, counts/s
SXR_QUALITY_COL = "sxr_quality"   # int8: 0 good, 1 flagged/interpolated/missing
HXR_QUALITY_COL = "hxr_quality"   # int8: 0 good, 1 flagged/interpolated/missing
REQUIRED_COLUMNS = [TIME_COL, SXR_COL, HXR_COL, SXR_QUALITY_COL, HXR_QUALITY_COL]

RESAMPLE_CADENCE_S = 1            # common 1 Hz grid

# --------------------------------------------------------------------------
# 3. INGESTION / CLEANING
# --------------------------------------------------------------------------
MERGE_TOLERANCE_S = 2            # align a raw sample to the grid within ±this
MAX_INTERPOLATION_GAP_S = 10     # interpolate gaps up to this; larger -> NaN+flag
SEGMENT_GAP_S = 3600             # gap above which data is a SEPARATE observing window
SAVE_PROCESSED = True

# SoLEXS Level-1 is counts/s, not GOES W/m^2. This linear scale maps counts to a
# GOES-equivalent soft X-ray flux so A-X classification is meaningful. It is a
# FITTABLE calibration (see calibrate.py), not a magic number; detection, ranking,
# lead time and the forecast model all work in count space and are unaffected.
# Default ANCHORED to the official NOAA/SWPC GOES list by a ROBUST (Huber) linear fit of
#   goes_peak_flux = k * our_peak_counts + b
# over **1,475** time-matched pairs (Feb-Aug 2024, complete 2,155-flare GOES list).
#   k = 1.898e-8      b = 2.7e-12  (negligible -> the zero-intercept assumption is fine)
# The earlier value (2.687e-8) came from a log-median of only 154 pairs against a TRUNCATED
# reference and over-classified: it produced 84 X-class flares (NOAA: 39) and 873 >=M (NOAA: 542).
# The refit brings those to 59 / 635 and lifts >=M purity 0.518 -> 0.676.
# Re-fit per dataset with scripts/fit_calibration.py.
SOLEXS_COUNTS_TO_FLUX = float(os.getenv("SOLARFLARE_SXR_SCALE", "1.898e-8"))

# --------------------------------------------------------------------------
# 4. FLARE CLASSIFICATION (GOES 1-8 A analogue, W/m^2)
# --------------------------------------------------------------------------
FLARE_CLASS_THRESHOLDS = {"A": 1e-8, "B": 1e-7, "C": 1e-6, "M": 1e-5, "X": 1e-4}
FLARE_CLASS_ORDER = ["A", "B", "C", "M", "X"]

# --------------------------------------------------------------------------
# 5. NOWCASTING (DETECTION)
# --------------------------------------------------------------------------
BACKGROUND_WINDOW_MIN = 480       # trailing rolling window for background level (8 h:
                                   # stable enough that extended activity does not pull
                                   # the baseline up and fragment a flare into pieces)
BACKGROUND_PERCENTILE = 5         # "quiet sun" percentile within that window
SMOOTH_WINDOW_S = 15              # short trailing smoothing of the raw signal
MIN_EVENT_DURATION_S = 120        # reject sub-2-minute blips (noise, not soft-X-ray flares)

# SXR trigger. The old ratio test (smooth / 5th-percentile-background >= 1.5) was UNSOUND:
# the background percentile of a Poisson-noisy series sits far below its mean, so the ratio is
# biased high on quiet Sun (measured: above threshold 62% of the time), and dividing by a
# near-zero percentile makes it explode (p99.9 = 4212). We now use a SIGNIFICANCE test against
# the analytic Poisson shot noise of the smoothed signal, sigma = sqrt(k*bg/N)  (see preprocess).
SXR_RISE_SIGMA = 12.0             # enter when excess >= this many sigma above background
SXR_EXIT_SIGMA = 6.0              # ...and the event ends when excess falls below this many sigma
SXR_RISE_THRESHOLD_RATIO = 1.5    # (legacy, retained for reference/back-compat only)
SXR_EXIT_THRESHOLD_RATIO = 1.2    # (legacy)
HXR_RISE_THRESHOLD_SIGMA = 3.5    # HXR triggers (enter) when excess >= N * past-noise sigma
HXR_EXIT_THRESHOLD_SIGMA = 2.0    # ...and ends when excess falls below this * noise
MIN_RISE_DURATION_S = 30          # SXR: sustained rise to reject noise spikes
HXR_MIN_RISE_DURATION_S = 8       # HXR is impulsive -> shorter sustain
PEAK_SEARCH_WINDOW_MIN = 120      # max time after trigger to look for the peak
END_DROP_FRACTION = 0.5           # event ends when excess decays to this * peak

# SXR episode splitting: one long elevated episode usually contains SEVERAL distinct
# flares. Without splitting they merge into a single episode whose one "peak" lands
# far from any real flare (hurts both completeness and lead-time accuracy). We split
# each episode at its local maxima so every flare becomes its own peak-aligned event.
SXR_SPLIT_EPISODES = True
SXR_PEAK_MIN_SEPARATION_S = 480      # distinct sub-flare peaks must be >= 8 min apart
# A real flare must RISE, not merely sit elevated. Requiring an ABSOLUTE prominence (in W/m^2)
# above the surrounding valleys — rather than a relative fraction of the episode's own scatter —
# is what removes the low-level over-detection. Tuned against the complete NOAA/SWPC list:
# raises >=C purity 0.354 -> 0.461 and >=C completeness 0.632 -> 0.684 (F1 0.453 -> 0.551).
SXR_PEAK_MIN_PROMINENCE_FLUX = 2e-6  # ~2x the C1 threshold
SXR_PEAK_PROMINENCE_STD_FRAC = 0.5   # (legacy, no longer used by the detector)
EVENT_GAP_BREAK_S = 300              # an episode is split wherever the data has a >5-min hole
                                     # (so one event can never span the gap between data months)

# Physical sanity bounds on event duration (the HXR multi-hour-event fix).
SXR_MAX_EVENT_DURATION_MIN = 360  # soft X-ray flares: minutes to a few hours
HXR_MAX_EVENT_DURATION_MIN = 30   # hard X-ray bursts: impulsive, seconds-minutes
HXR_REFRACTORY_S = 60             # min quiet gap before a new HXR event can open

COINCIDENCE_WINDOW_MIN = 10       # SXR<->HXR matching window for fusion

# --------------------------------------------------------------------------
# 6. FORECASTING
# --------------------------------------------------------------------------
FORECAST_HORIZON_MIN = 30         # predict P(flare) in the next N minutes
LOOKBACK_WINDOW_MIN = 60          # feature history window
CONTEXT_WINDOW_MIN = 360          # longer activity baseline (flares cluster)
FEATURE_STEP_MIN = 1              # sliding-window stride
MIN_FLARE_CLASS_FOR_POSITIVE = "C"  # label "1" only for C-class and above

# WHAT the forecaster is trained to predict.
#   "goes" — the official NOAA/SWPC flare list (independent ground truth). This is the correct,
#            non-circular choice: the model predicts REAL flares and its scores are comparable to
#            published forecasters.
#   "self" — our own detector's catalogue. Circular (the model learns to predict its own
#            detector), inflates the base rate, and makes metrics un-comparable. Ablation only.
FORECAST_LABEL_SOURCE = os.getenv("SOLARFLARE_LABEL_SOURCE", "goes")

OPTIMIZE_ALERT_THRESHOLD = True   # pick threshold on validation (F1), apply to test
ALERT_PROBABILITY_THRESHOLD = 0.5 # fallback fixed threshold

TRAIN_FRACTION = 0.70
VAL_FRACTION = 0.15               # remaining 0.15 -> test (chronological, never shuffled)
RANDOM_SEED = 42

# --------------------------------------------------------------------------
# 7. API / OPS
# --------------------------------------------------------------------------
API_KEY = os.getenv("SOLARFLARE_API_KEY", "")        # empty -> auth disabled
AUTO_INGEST = os.getenv("SOLARFLARE_AUTO_INGEST", "1") == "1"
CHECK_INTERVAL_S = int(os.getenv("SOLARFLARE_CHECK_INTERVAL", "60"))
