"""
nowcast.py — real-time flare DETECTION and classification.

A trailing-window state machine runs independently on the soft (SXR) and hard
(HXR) X-ray channels (QUIET -> RISING -> DECAYING -> QUIET), then the two
catalogues are fused by temporal coincidence into a master catalogue:

  confirmed  both channels      (high confidence; thermal + non-thermal)
  sxr_only   soft only          (medium)
  hxr_only   hard only          (low; impulsive burst)

Hardening: each channel enforces a maximum event duration and a refractory gap,
so the HXR detector can no longer emit a physically impossible multi-hour
"hard X-ray flare".
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .preprocess import add_features


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def classify_flare(peak_flux: float) -> str:
    """Map a peak soft X-ray flux (W/m^2) to a GOES class string, e.g. 'C7.8'."""
    if peak_flux is None or not np.isfinite(peak_flux) or peak_flux <= 0:
        return "Unclassified"
    th = config.FLARE_CLASS_THRESHOLDS
    for letter in reversed(config.FLARE_CLASS_ORDER):       # X, M, C, B, A
        base = th[letter]
        if peak_flux >= base:
            return f"{letter}{peak_flux / base:.1f}"
    return f"A{peak_flux / th['A']:.1f}"                    # below A -> sub-A


# --------------------------------------------------------------------------
# Generic state-machine detector
# --------------------------------------------------------------------------
def _runs(active: np.ndarray) -> list[tuple[int, int]]:
    """Return [(start_idx, length), ...] for each contiguous True run in ``active``."""
    idx = np.flatnonzero(active)
    if idx.size == 0:
        return []
    splits = np.flatnonzero(np.diff(idx) > 1)
    groups = np.split(idx, splits + 1)
    return [(int(g[0]), int(g.size)) for g in groups]


def _break_at_gaps(t_s: np.ndarray, start: int, end: int, gap_s: float) -> list[tuple[int, int]]:
    """Split an index range [start, end] wherever consecutive samples are > ``gap_s`` apart."""
    pieces: list[tuple[int, int]] = []
    s = start
    for i in range(start, end):
        if t_s[i + 1] - t_s[i] > gap_s:
            pieces.append((s, i))
            s = i + 1
    pieces.append((s, end))
    return pieces


def _hysteresis(enter: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """Schmitt-trigger active mask: turn on when ``enter`` fires, stay on until ``exit_`` fires.

    This makes one elevated episode a single active region, so noise wiggles around
    the rise threshold do not fragment a flare, while a return to background (exit)
    cleanly separates distinct flares.
    """
    n = len(enter)
    active = np.zeros(n, dtype=bool)
    on = False
    for i in range(n):
        if on:
            if exit_[i]:
                on = False
            else:
                active[i] = True
        elif enter[i]:
            on = True
            active[i] = True
    return active


def _split_episode(seg: np.ndarray, start: int) -> list[tuple[int, int, int]]:
    """Split one elevated episode into (onset, peak, end) triples, one per local flare.

    ``seg`` is the excess over the episode; ``start`` is its absolute start index. Peaks
    are found with a minimum separation and prominence so only genuinely distinct flares
    split out; the boundary between two peaks is the valley (minimum) between them
    (a watershed split). Returns absolute indices. Falls back to a single argmax peak.
    """
    from scipy.signal import find_peaks

    filled = np.where(np.isfinite(seg), seg, np.nanmin(seg[np.isfinite(seg)]) if np.isfinite(seg).any() else 0.0)
    # ABSOLUTE prominence: a real flare must RISE by a physically meaningful amount above its
    # surrounding valleys. A relative (fraction-of-std) prominence let low-level wiggles through
    # and was the main source of over-detection at C-class.
    pk, _ = find_peaks(filled, distance=max(1, config.SXR_PEAK_MIN_SEPARATION_S),
                       prominence=config.SXR_PEAK_MIN_PROMINENCE_FLUX)
    if len(pk) == 0:
        pk = np.array([int(np.nanargmax(filled))])

    triples = []
    for k, p in enumerate(pk):
        onset = 0 if k == 0 else int(pk[k - 1] + np.argmin(filled[pk[k - 1]:p + 1]))
        end = len(filled) - 1 if k == len(pk) - 1 else int(p + np.argmin(filled[p:pk[k + 1] + 1]))
        triples.append((start + onset, start + int(p), start + end))
    return triples


def detect_events(time: pd.DatetimeIndex, active: np.ndarray, excess: np.ndarray,
                  level: np.ndarray, *, min_rise_s: int, max_duration_s: int,
                  split: bool = False) -> list[dict]:
    """Turn hysteresis active regions into flare events.

    Hysteresis (``active``) groups one elevated episode, so noise does not fragment a
    flare and a return to background cleanly separates temporally-distinct flares. When
    ``split`` is set, an episode that contains several local peaks is divided into one
    event per peak (watershed split at the valley between peaks), so flares riding on a
    larger flare's decay tail — or a long active period holding many flares — are resolved
    individually instead of merging into one episode with a single, misplaced peak.
    """
    n = len(time)
    # epoch SECONDS, unit-safe across pandas/numpy datetime resolutions (ns vs us)
    t_s = time.values.astype("datetime64[s]").astype("int64")
    events: list[dict] = []
    for start, length in _runs(active):
        run_end = min(start + length - 1, n - 1)
        # break the episode wherever the data has a hole, so one event can never span the
        # multi-week gap between data months (which otherwise yields a 60-day "flare").
        for s0, e0 in _break_at_gaps(t_s, start, run_end, config.EVENT_GAP_BREAK_S):
            if (e0 - s0 + 1) < min_rise_s:
                continue
            if split:
                spans = _split_episode(excess[s0:e0 + 1], s0)
            else:
                peak = s0 + int(np.nanargmax(excess[s0:e0 + 1]))
                spans = [(s0, peak, e0)]
            for onset, peak, end in spans:
                # cap the event to max_duration_s of ELAPSED TIME (robust to small holes)
                cap_idx = onset + int(np.searchsorted(t_s[onset:end + 1],
                                                      t_s[onset] + max_duration_s, side="right")) - 1
                end = min(end, max(onset, cap_idx), n - 1)
                if peak > end:
                    peak = onset + int(np.nanargmax(excess[onset:end + 1]))
                events.append({
                    "onset_idx": onset, "peak_idx": peak, "end_idx": end,
                    "onset_time": time[onset], "peak_time": time[peak], "end_time": time[end],
                    "peak_excess": float(excess[peak]), "peak_level": float(level[peak]),
                    "duration_min": (time[end] - time[onset]).total_seconds() / 60.0,
                })
    return events


# --------------------------------------------------------------------------
# Per-channel wrappers
# --------------------------------------------------------------------------
def detect_sxr(feat: pd.DataFrame) -> list[dict]:
    time = pd.DatetimeIndex(feat[config.TIME_COL])
    excess = feat["sxr_excess"].to_numpy()
    noise = feat["sxr_noise"].to_numpy()        # Poisson shot noise (see preprocess)
    level = feat["sxr_smooth"].to_numpy()       # absolute flux for classification
    # SIGNIFICANCE trigger (not the old smooth/background ratio, which was biased high on quiet
    # Sun by the Poisson percentile effect and fired 62% of the time).
    enter = np.nan_to_num(excess >= config.SXR_RISE_SIGMA * noise, nan=False)
    exit_ = np.nan_to_num(excess < config.SXR_EXIT_SIGMA * noise, nan=True)
    active = _hysteresis(enter, exit_)
    events = detect_events(time, active, excess, level,
                           min_rise_s=config.MIN_RISE_DURATION_S,
                           max_duration_s=config.SXR_MAX_EVENT_DURATION_MIN * 60,
                           split=config.SXR_SPLIT_EPISODES)
    # soft X-ray flares last minutes; drop sub-threshold blips
    return [e for e in events if e["duration_min"] * 60 >= config.MIN_EVENT_DURATION_S]


def detect_hxr(feat: pd.DataFrame) -> list[dict]:
    time = pd.DatetimeIndex(feat[config.TIME_COL])
    excess = feat["hxr_excess"].to_numpy()
    noise = feat["hxr_noise"].to_numpy()
    level = feat["hxr_smooth"].to_numpy()        # counts/s
    enter = excess >= config.HXR_RISE_THRESHOLD_SIGMA * noise
    exit_ = excess < config.HXR_EXIT_THRESHOLD_SIGMA * noise
    active = _hysteresis(enter, exit_)
    return detect_events(time, active, excess, level,
                         min_rise_s=config.HXR_MIN_RISE_DURATION_S,
                         max_duration_s=config.HXR_MAX_EVENT_DURATION_MIN * 60)  # multi-hour fix


# --------------------------------------------------------------------------
# Fusion -> master catalogue
# --------------------------------------------------------------------------
def fuse(sxr_events: list[dict], hxr_events: list[dict]) -> pd.DataFrame:
    """Combine SXR + HXR detections into a master catalogue with confidence tiers."""
    window = pd.Timedelta(minutes=config.COINCIDENCE_WINDOW_MIN)
    hxr_used = set()
    rows = []

    for se in sxr_events:
        match, best = None, window
        for j, he in enumerate(hxr_events):
            if j in hxr_used:
                continue
            # HXR burst near the SXR onset..peak interval
            lo = se["onset_time"] - window
            hi = se["peak_time"] + window
            if lo <= he["peak_time"] <= hi:
                gap = abs(he["peak_time"] - se["peak_time"])
                if gap <= best:
                    best, match = gap, j
        if match is not None:
            hxr_used.add(match)
            he = hxr_events[match]
            lead = (se["peak_time"] - he["peak_time"]).total_seconds() / 60.0
            rows.append(_row("confirmed", "high", se, he, lead))
        else:
            rows.append(_row("sxr_only", "medium", se, None, None))

    for j, he in enumerate(hxr_events):
        if j not in hxr_used:
            rows.append(_row("hxr_only", "low", None, he, None))

    cols = ["event_id", "status", "confidence", "onset_time", "peak_time_sxr",
            "peak_time_hxr", "end_time", "flare_class", "peak_sxr_flux_w_m2",
            "peak_hxr_counts_s", "hxr_leads_sxr_minutes"]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df = df.sort_values("onset_time").reset_index(drop=True)
    df["event_id"] = [f"FLR_{i + 1:04d}" for i in range(len(df))]
    return df


def _row(status: str, confidence: str, se: dict | None, he: dict | None,
         lead: float | None) -> dict:
    onset = (se or he)["onset_time"]
    end = (se or he)["end_time"]
    flux = se["peak_level"] if se else None
    return {
        "event_id": "", "status": status, "confidence": confidence,
        "onset_time": onset,
        "peak_time_sxr": se["peak_time"] if se else None,
        "peak_time_hxr": he["peak_time"] if he else None,
        "end_time": end,
        "flare_class": classify_flare(flux) if se else "Unclassified (HXR-only)",
        "peak_sxr_flux_w_m2": flux,
        "peak_hxr_counts_s": he["peak_level"] if he else None,
        "hxr_leads_sxr_minutes": round(lead, 2) if lead is not None else None,
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run_nowcast(df: pd.DataFrame) -> dict:
    """Detect flares on a canonical light curve and return catalogues.

    Returns {'master', 'sxr', 'hxr', 'features'} where master/sxr/hxr are
    DataFrames and features is the enriched light curve (reused by forecasting).
    """
    feat = add_features(df)
    sxr_events = detect_sxr(feat)
    hxr_events = detect_hxr(feat)
    master = fuse(sxr_events, hxr_events)
    return {
        "master": master,
        "sxr": pd.DataFrame(sxr_events),
        "hxr": pd.DataFrame(hxr_events),
        "features": feat,
    }
