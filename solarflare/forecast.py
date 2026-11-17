"""
forecast.py — predict P(flare in the next N minutes) with quantified lead time.

Framed as causal binary classification on a sliding window:
  X(t) = features over the past LOOKBACK (+ CONTEXT) minutes  (trailing only)
  y(t) = 1 if a flare of class >= MIN_FLARE_CLASS_FOR_POSITIVE starts within the
            next FORECAST_HORIZON_MIN minutes, else 0

Leakage discipline:
  * features are trailing-window only (no future peeking)
  * chronological train/val/test split (never shuffled)
  * alert threshold chosen on validation, reported on unseen test
  * evaluation is flare-based (TPR / FAR / lead time before peak), the PS-15 metric
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from . import config

CLASS_RANK = {c: i for i, c in enumerate(config.FLARE_CLASS_ORDER)}  # A0 B1 C2 M3 X4


# --------------------------------------------------------------------------
# Feature engineering (per-minute, causal)
# --------------------------------------------------------------------------
def _minute_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Resample the 1-Hz canonical curve to per-minute aggregates."""
    s = df.set_index(config.TIME_COL)
    mt = pd.DataFrame({
        "sxr_mean": s[config.SXR_COL].resample("1min").mean(),
        "sxr_max": s[config.SXR_COL].resample("1min").max(),
        "hxr_mean": s[config.HXR_COL].resample("1min").mean(),
        "hxr_max": s[config.HXR_COL].resample("1min").max(),
    })
    return mt


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build the per-minute causal feature matrix (index = minute timestamps)."""
    mt = _minute_frame(df)
    lb = config.LOOKBACK_WINDOW_MIN
    ctx = config.CONTEXT_WINDOW_MIN
    feats = {}

    for c in ("sxr", "hxr"):
        m = mt[f"{c}_mean"]
        x = mt[f"{c}_max"]
        r = m.rolling(lb, min_periods=max(2, lb // 4))
        feats[f"{c}_lb_mean"] = r.mean()
        feats[f"{c}_lb_std"] = r.std()
        feats[f"{c}_lb_max"] = x.rolling(lb, min_periods=max(2, lb // 4)).max()
        feats[f"{c}_lb_slope"] = (m - m.shift(lb - 1)) / lb
        feats[f"{c}_lb_maxjump"] = m.diff().abs().rolling(lb, min_periods=2).max()
        # context: activity / clustering over a longer window
        cr = m.rolling(ctx, min_periods=max(2, ctx // 6))
        bg = cr.quantile(0.10)
        feats[f"{c}_ctx_mean"] = cr.mean()
        feats[f"{c}_ctx_max"] = x.rolling(ctx, min_periods=max(2, ctx // 6)).max()
        # `(NaN > NaN) -> False -> astype(float) -> 0.0`, so this silently FABRICATED a valid
        # 0.0 for every minute we never observed. It made all 43,200 minutes of June 2024 (a
        # month with zero data) look like real, quiet observations — they survived the NaN
        # filter, were scored against real GOES labels, and polluted the test window.
        # Keep NaN as NaN.
        active = (m > (bg * 1.5)).astype(float).where(m.notna())
        feats[f"{c}_ctx_activity"] = active.rolling(ctx, min_periods=max(2, ctx // 6)).mean()

    feats["sxr_hxr_xcorr"] = (
        mt["sxr_mean"].rolling(lb, min_periods=max(3, lb // 4)).corr(mt["hxr_mean"])
    )

    fdf = pd.DataFrame(feats, index=mt.index)
    return fdf


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------
def _qualifying_onsets(flares: pd.DataFrame) -> np.ndarray:
    """Sorted epoch-second onsets of flares at/above MIN_FLARE_CLASS_FOR_POSITIVE."""
    if flares is None or flares.empty:
        return np.empty(0, dtype="int64")
    min_rank = CLASS_RANK[config.MIN_FLARE_CLASS_FOR_POSITIVE]
    cls = flares["flare_class"].astype(str).str[:1]
    keep = cls.map(lambda c: CLASS_RANK.get(c, -1) >= min_rank).to_numpy()
    onsets = pd.to_datetime(flares.loc[keep, "onset_time"], utc=True, errors="coerce").dropna()
    if onsets.empty:
        return np.empty(0, dtype="int64")
    return np.sort(pd.DatetimeIndex(onsets).values.astype("datetime64[s]").astype("int64"))


def build_labels(index: pd.DatetimeIndex, flares: pd.DataFrame) -> pd.Series:
    """y(t)=1 if a qualifying flare starts within the next horizon minutes.

    Vectorised with searchsorted: the previous per-flare boolean-mask loop was
    O(n_flares x n_minutes) (~650 M comparisons with the full GOES list) and dominated ingest.
    """
    h = config.FORECAST_HORIZON_MIN
    pos = pd.DatetimeIndex(index).values.astype("datetime64[s]").astype("int64")
    onsets = _qualifying_onsets(flares)
    if onsets.size == 0:
        return pd.Series(np.zeros(len(index), dtype="int8"), index=index, dtype="int8")
    # count onsets in (t, t + h]  -> a flare starts within the next h minutes
    hi = np.searchsorted(onsets, pos + h * 60, side="right")
    lo = np.searchsorted(onsets, pos, side="right")
    return pd.Series(((hi - lo) > 0).astype("int8"), index=index, dtype="int8")


# --------------------------------------------------------------------------
# Evaluation (flare-based)
# --------------------------------------------------------------------------
def _episodes(alert_times: pd.DatetimeIndex, gap_min: int = 5) -> list[tuple]:
    """Collapse alert minutes into (start, end) episodes (merge gaps <= gap_min)."""
    if len(alert_times) == 0:
        return []
    t = pd.DatetimeIndex(sorted(alert_times))
    eps, start, prev = [], t[0], t[0]
    for cur in t[1:]:
        if (cur - prev) > pd.Timedelta(minutes=gap_min):
            eps.append((start, prev))
            start = cur
        prev = cur
    eps.append((start, prev))
    return eps


def evaluate(timeline: pd.DataFrame, master: pd.DataFrame, threshold: float,
             t0: pd.Timestamp, t1: pd.Timestamp) -> dict:
    """Flare-based TPR / FAR / lead-time over the window [t0, t1]."""
    h = config.FORECAST_HORIZON_MIN
    min_rank = CLASS_RANK[config.MIN_FLARE_CLASS_FOR_POSITIVE]
    seg = timeline[(timeline["time"] >= t0) & (timeline["time"] <= t1)].dropna(subset=["proba"])
    alert_times = pd.DatetimeIndex(seg.loc[seg["proba"] >= threshold, "time"])

    flares = []
    for _, row in master.iterrows():
        cls = str(row.get("flare_class", ""))
        if not cls or cls[0] not in CLASS_RANK or CLASS_RANK[cls[0]] < min_rank:
            continue
        onset = pd.Timestamp(row["onset_time"])
        if not (t0 <= onset <= t1):
            continue
        peak = row.get("peak_time_sxr")
        peak = pd.Timestamp(peak) if pd.notna(peak) else onset
        flares.append((onset, peak))

    leads_peak, leads_onset, hits = [], [], 0
    for onset, peak in flares:
        win = alert_times[(alert_times >= onset - pd.Timedelta(minutes=h)) & (alert_times <= onset)]
        if len(win):
            hits += 1
            first = win.min()
            leads_onset.append((onset - first).total_seconds() / 60.0)
            leads_peak.append((peak - first).total_seconds() / 60.0)

    eps = _episodes(alert_times)
    false_eps = 0
    for s, e in eps:
        # an episode is a true alarm if a qualifying flare onsets within [s, e+h]
        ok = any(s - pd.Timedelta(minutes=h) <= o <= e + pd.Timedelta(minutes=h) for o, _ in flares)
        if not ok:
            false_eps += 1

    n = len(flares)
    return {
        "n_flares": n,
        "tpr": (hits / n) if n else float("nan"),
        "n_episodes": len(eps),
        "far": (false_eps / len(eps)) if eps else float("nan"),
        "median_lead_peak_min": float(np.median(leads_peak)) if leads_peak else float("nan"),
        "median_lead_onset_min": float(np.median(leads_onset)) if leads_onset else float("nan"),
        "mean_lead_peak_min": float(np.mean(leads_peak)) if leads_peak else float("nan"),
    }


def _skill_baselines(test_index: pd.DatetimeIndex, y_true: np.ndarray, proba: np.ndarray,
                     threshold: float, base_rate: float, master: pd.DataFrame) -> dict:
    """Does the model actually beat trivial baselines on the SAME test window?

    Reports the **True Skill Statistic** (TSS = TPR - FPR, the standard operational
    flare-forecasting score) for the model vs a **persistence** baseline (a qualifying
    flare just occurred -> predict another), and the **Brier Skill Score** vs a
    **climatology** baseline (always predict the training base rate). Skill is real when
    model TSS > persistence TSS and BSS > 0.
    """
    y = np.asarray(y_true).astype(bool)
    h = config.FORECAST_HORIZON_MIN
    min_rank = CLASS_RANK[config.MIN_FLARE_CLASS_FOR_POSITIVE]

    def tss(pred: np.ndarray) -> float:
        pred = np.asarray(pred).astype(bool)
        tp = int(np.sum(pred & y)); fn = int(np.sum(~pred & y))
        fp = int(np.sum(pred & ~y)); tn = int(np.sum(~pred & ~y))
        tpr = tp / (tp + fn) if (tp + fn) else 0.0
        fpr = fp / (fp + tn) if (fp + tn) else 0.0
        return tpr - fpr

    # persistence: predict 1 if a qualifying flare onset fell in the previous h minutes
    onsets = [pd.Timestamp(r["onset_time"]) for _, r in master.iterrows()
              if str(r.get("flare_class", ""))[:1] in CLASS_RANK
              and CLASS_RANK[str(r.get("flare_class", ""))[:1]] >= min_rank]
    pos = pd.DatetimeIndex(test_index).values.astype("datetime64[s]").astype("int64")
    persist = np.zeros(len(pos), dtype=bool)
    if onsets:
        o = pd.DatetimeIndex(sorted(onsets)).values.astype("datetime64[s]").astype("int64")
        hi = np.searchsorted(o, pos, side="right")
        lo = np.searchsorted(o, pos - h * 60, side="right")
        persist = (hi - lo) > 0

    brier_model = float(np.mean((proba - np.asarray(y_true)) ** 2))
    brier_clim = float(np.mean((base_rate - np.asarray(y_true)) ** 2))
    bss = (1 - brier_model / brier_clim) if brier_clim > 0 else float("nan")
    m_tss, p_tss = tss(proba >= threshold), tss(persist)
    return {
        "model_tss": round(m_tss, 3),
        "persistence_tss": round(p_tss, 3),
        "beats_persistence": bool(m_tss > p_tss),
        "brier_skill_score": round(bss, 3),
        "beats_climatology": bool(bss > 0),
        "base_rate": round(float(base_rate), 4),
    }


def _best_threshold_f1(y_true: np.ndarray, proba: np.ndarray) -> float:
    """Threshold maximizing F1 on (y_true, proba)."""
    best_t, best_f1 = 0.5, -1.0
    for t in np.linspace(0.05, 0.95, 19):
        pred = proba >= t
        tp = np.sum(pred & (y_true == 1))
        fp = np.sum(pred & (y_true == 0))
        fn = np.sum(~pred & (y_true == 1))
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        if f1 > best_f1:
            best_f1, best_t = f1, float(t)
    return best_t


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run_forecast(df: pd.DataFrame, master: pd.DataFrame,
                 targets: pd.DataFrame | None = None) -> dict:
    """Train, evaluate (chronological, leakage-safe) and produce the forecast timeline.

    ``targets`` are the flares the model is trained to PREDICT and is SCORED against. When the
    official NOAA/SWPC GOES list is supplied (the default, see config.FORECAST_LABEL_SOURCE), the
    task is real and the scores are comparable to published forecasters. Falling back to ``master``
    (our own detections) is circular — the model then predicts its own detector — and is only
    meaningful as an ablation.
    """
    if targets is None or targets.empty:
        targets = master
    fdf = build_features(df)
    y = build_labels(fdf.index, targets)

    # Do NOT dropna(). HistGradientBoosting has NATIVE missing-value support, and dropping every
    # row that lacks a hard-X-ray feature silently discarded 46.8% of the timeline (142,868 of
    # 305,280 rows) — including rows with perfectly good soft-X-ray data. Keeping them doubles
    # the training set and improved the Brier Skill Score ~10x in testing.
    #
    # BUT: only predict for minutes where we ACTUALLY OBSERVED the Sun. `build_features` resamples
    # onto a continuous 1-minute grid, so months with no data at all (June 2024) still appear as
    # rows. Requiring a real trailing soft-X-ray mean (sxr_lb_mean) drops them: we must never
    # score a forecast for a period the instrument was not looking.
    valid = fdf[fdf["sxr_lb_mean"].notna()]
    y = y.loc[valid.index]
    X = valid
    n = len(X)
    if n < 200 or y.sum() < 5:
        return {"ok": False, "reason": f"insufficient data (n={n}, positives={int(y.sum())})"}

    i_tr = int(n * config.TRAIN_FRACTION)
    i_val = int(n * (config.TRAIN_FRACTION + config.VAL_FRACTION))
    Xtr, ytr = X.iloc[:i_tr], y.iloc[:i_tr]
    Xval, yval = X.iloc[i_tr:i_val], y.iloc[i_tr:i_val]
    Xte, yte = X.iloc[i_val:], y.iloc[i_val:]

    model = HistGradientBoostingClassifier(
        random_state=config.RANDOM_SEED, max_iter=300, learning_rate=0.06,
        l2_regularization=1.0, class_weight="balanced",
    )
    model.fit(Xtr.to_numpy(), ytr.to_numpy())

    proba_val = model.predict_proba(Xval.to_numpy())[:, 1] if len(Xval) else np.array([])
    proba_te = model.predict_proba(Xte.to_numpy())[:, 1] if len(Xte) else np.array([])

    val_auc = roc_auc_score(yval, proba_val) if len(set(yval)) > 1 else float("nan")
    test_auc = roc_auc_score(yte, proba_te) if len(set(yte)) > 1 else float("nan")

    if config.OPTIMIZE_ALERT_THRESHOLD and len(proba_val) and len(set(yval)) > 1:
        threshold = _best_threshold_f1(yval.to_numpy(), proba_val)
    else:
        threshold = config.ALERT_PROBABILITY_THRESHOLD

    # full-timeline probability for the dashboard (train portion is in-sample; flagged)
    proba_all = model.predict_proba(X.to_numpy())[:, 1]
    timeline = pd.DataFrame({"time": X.index, "proba": proba_all})
    timeline["in_sample"] = timeline["time"] < X.index[i_val]

    test_metrics = evaluate(timeline, targets, threshold, X.index[i_val], X.index[-1])

    # skill vs trivial baselines (persistence + climatology) on the same test window
    skill = _skill_baselines(Xte.index, yte.to_numpy(), proba_te, threshold,
                             float(ytr.mean()), targets) if len(proba_te) else {}

    # alert episodes over the whole timeline (for the catalogue / UI)
    alert_times = pd.DatetimeIndex(timeline.loc[timeline["proba"] >= threshold, "time"])
    eps = _episodes(alert_times)
    alerts = pd.DataFrame([
        {"alert_id": f"ALT_{i + 1:04d}", "start_time": s, "end_time": e,
         "peak_probability": float(
             timeline.loc[(timeline["time"] >= s) & (timeline["time"] <= e), "proba"].max())}
        for i, (s, e) in enumerate(eps)
    ])

    return {
        "ok": True, "model": model, "threshold": threshold,
        "val_auc": val_auc, "test_auc": test_auc,
        "timeline": timeline, "alerts": alerts,
        "feature_names": list(X.columns),
        "n_samples": n, "n_positive": int(y.sum()),
        "test_metrics": test_metrics,
        "skill": skill,
        "splits": {"train_end": str(X.index[i_tr]), "val_end": str(X.index[i_val])},
    }
