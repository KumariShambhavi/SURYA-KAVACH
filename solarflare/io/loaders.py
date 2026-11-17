"""
loaders.py — read converted SoLEXS/HEL1OS CSVs and build the canonical light curve.

Pipeline:
  1. read all data/raw/solexs/*.csv (ISOT, COUNTS) and data/raw/hel1os/*.csv (ISOT, CTR)
  2. parse time (UTC), drop NaN samples, deduplicate timestamps
  3. split into contiguous observing SEGMENTS (gaps > SEGMENT_GAP_S start a new one)
  4. per segment, align both channels onto a common 1-Hz grid (nearest within tol)
  5. interpolate gaps up to MAX_INTERPOLATION_GAP_S; flag missing/interpolated samples
  6. scale SoLEXS counts -> GOES-equivalent flux
  7. return one canonical DataFrame [time, sxr_flux, hxr_flux, sxr_quality, hxr_quality]
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config
from ..schema import validate_light_curve


def _read_csv_series(paths: list[str], value_col: str) -> pd.Series:
    """Read+concat CSVs with columns [ISOT, value_col] -> a clean time-indexed Series."""
    frames = []
    for p in paths:
        df = pd.read_csv(p, usecols=["ISOT", value_col])
        frames.append(df)
    if not frames:
        return pd.Series(dtype="float64")
    df = pd.concat(frames, ignore_index=True)
    t = pd.to_datetime(df["ISOT"], utc=True, format="ISO8601", errors="coerce")
    v = pd.to_numeric(df[value_col], errors="coerce")
    s = pd.Series(v.to_numpy(), index=t)
    s = s[~s.index.isna()].dropna()
    if s.empty:
        return s
    # deduplicate identical timestamps by averaging, then sort
    s = s.groupby(level=0).mean().sort_index()
    return s


def load_raw_frames(solexs_dir: Path | None = None,
                    hel1os_dir: Path | None = None) -> tuple[pd.Series, pd.Series]:
    """Return (sxr_counts, hxr_ctr) raw time-indexed Series from converted CSVs."""
    sdir = Path(solexs_dir or config.SOLEXS_RAW_DIR)
    hdir = Path(hel1os_dir or config.HEL1OS_RAW_DIR)
    sxr = _read_csv_series(sorted(glob.glob(str(sdir / "*.csv"))), "COUNTS")
    hxr = _read_csv_series(sorted(glob.glob(str(hdir / "*.csv"))), "CTR")
    return sxr, hxr


def _segments(index: pd.DatetimeIndex, gap_s: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Split a sorted DatetimeIndex into [start, end] spans wherever the gap exceeds gap_s."""
    if len(index) == 0:
        return []
    diffs = index.to_series().diff().dt.total_seconds().to_numpy()
    breaks = np.where(diffs > gap_s)[0]
    bounds = [0, *breaks.tolist(), len(index)]
    spans = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        spans.append((index[a], index[b - 1]))
    return spans


def _align_segment(raw: pd.Series, grid: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """Align ``raw`` onto ``grid`` (nearest within tolerance), interpolate small gaps.

    Returns (values, quality) where quality is 1 for missing/interpolated samples.
    """
    seg = raw[(raw.index >= grid[0]) & (raw.index <= grid[-1])]
    if seg.empty:
        n = len(grid)
        return np.full(n, np.nan), np.ones(n, dtype="int8")
    aligned = seg.reindex(grid, method="nearest",
                          tolerance=pd.Timedelta(seconds=config.MERGE_TOLERANCE_S))
    quality = aligned.isna().to_numpy().astype("int8")  # 1 where no real sample
    filled = aligned.interpolate(method="linear",
                                 limit=config.MAX_INTERPOLATION_GAP_S,
                                 limit_area="inside")
    return filled.to_numpy(), quality


def build_light_curve(solexs_dir: Path | None = None,
                      hel1os_dir: Path | None = None,
                      save: bool | None = None) -> pd.DataFrame:
    """Build and return the canonical light-curve DataFrame from converted CSVs."""
    sxr_raw, hxr_raw = load_raw_frames(solexs_dir, hel1os_dir)
    if sxr_raw.empty and hxr_raw.empty:
        raise FileNotFoundError(
            f"No converted CSVs found in {config.SOLEXS_RAW_DIR} / {config.HEL1OS_RAW_DIR}. "
            "Run the PRADAN conversion first (solarflare convert)."
        )

    # Union of timestamps drives segmentation so both channels share segment bounds.
    all_times = sxr_raw.index.union(hxr_raw.index)
    spans = _segments(all_times, config.SEGMENT_GAP_S)

    out_frames = []
    for start, end in spans:
        grid = pd.date_range(start.floor("s"), end.ceil("s"),
                             freq=f"{config.RESAMPLE_CADENCE_S}s", tz="UTC")
        if len(grid) == 0:
            continue
        sxr_counts, sxr_q = _align_segment(sxr_raw, grid)
        hxr_vals, hxr_q = _align_segment(hxr_raw, grid)
        out_frames.append(pd.DataFrame({
            config.TIME_COL: grid,
            config.SXR_COL: sxr_counts * config.SOLEXS_COUNTS_TO_FLUX,
            config.HXR_COL: hxr_vals,
            config.SXR_QUALITY_COL: sxr_q,
            config.HXR_QUALITY_COL: hxr_q,
        }))

    df = pd.concat(out_frames, ignore_index=True) if out_frames else pd.DataFrame(
        columns=config.REQUIRED_COLUMNS)
    df = df.sort_values(config.TIME_COL).reset_index(drop=True)
    df[config.SXR_QUALITY_COL] = df[config.SXR_QUALITY_COL].astype("int8")
    df[config.HXR_QUALITY_COL] = df[config.HXR_QUALITY_COL].astype("int8")

    validate_light_curve(df)

    if save if save is not None else config.SAVE_PROCESSED:
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(config.PROCESSED_LIGHT_CURVE, index=False)
    return df
