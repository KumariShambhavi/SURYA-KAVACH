"""
pipeline.py — orchestrate the full science pipeline and write artifacts.

build canonical light curve -> nowcast (detect + fuse) -> forecast (train + eval)
-> write outputs/ (catalogues, timeline, alerts, model, summary) and return a
machine-readable summary dict.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import joblib
import pandas as pd

from . import config

log = logging.getLogger("solarflare.pipeline")
from .io import build_light_curve
from .nowcast import run_nowcast
from .forecast import run_forecast


def _forecast_targets(df: pd.DataFrame) -> pd.DataFrame | None:
    """The flares the forecaster is trained on and scored against.

    Default is the official NOAA/SWPC GOES list — an INDEPENDENT ground truth. Training on our
    own detections instead would be circular (the model learns to predict its own detector) and
    would make the scores un-comparable to published forecasters.
    """
    if config.FORECAST_LABEL_SOURCE != "goes":
        return None
    try:
        from .validate import goes_targets
        t0 = df[config.TIME_COL].min().to_pydatetime()
        t1 = df[config.TIME_COL].max().to_pydatetime()
        tg = goes_targets(t0, t1)
        if tg is not None and len(tg):
            log.info("forecast targets: %d official GOES flares", len(tg))
            return tg
        log.warning("GOES targets unavailable -> falling back to self-labels (CIRCULAR)")
    except Exception as e:  # noqa: BLE001 - never let this break the pipeline
        log.warning("GOES targets failed (%s) -> falling back to self-labels (CIRCULAR)", e)
    return None


def run_pipeline(save: bool = True) -> dict:
    config.ensure_dirs()
    df = build_light_curve(save=save)
    now = run_nowcast(df)
    master, sxr, hxr = now["master"], now["sxr"], now["hxr"]
    targets = _forecast_targets(df)
    fc = run_forecast(df, master, targets=targets)

    summary = {
        "samples": int(len(df)),
        "time_start": str(df[config.TIME_COL].min()),
        "time_end": str(df[config.TIME_COL].max()),
        "sxr_events": int(len(sxr)),
        "hxr_events": int(len(hxr)),
        "master_events": int(len(master)),
        "status_counts": (master["status"].value_counts().to_dict() if len(master) else {}),
        "forecast_ok": bool(fc.get("ok", False)),
        "forecast_label_source": ("GOES (independent)" if targets is not None else "self (circular)"),
        "forecast_n_targets": int(len(targets)) if targets is not None else int(len(master)),
    }
    if fc.get("ok"):
        summary.update({
            "val_auc": fc["val_auc"], "test_auc": fc["test_auc"],
            "alert_threshold": fc["threshold"],
            "n_forecast_alerts": int(len(fc["alerts"])),
            **{f"test_{k}": v for k, v in fc["test_metrics"].items()},
            **{f"skill_{k}": v for k, v in fc.get("skill", {}).items()},
        })

    if save:
        out = config.OUTPUT_DIR
        out.mkdir(parents=True, exist_ok=True)
        if len(master):
            master.to_csv(out / "nowcast_master_catalogue.csv", index=False)
        _save_events(sxr, out / "nowcast_sxr_events.csv")
        _save_events(hxr, out / "nowcast_hxr_events.csv")
        if fc.get("ok"):
            fc["timeline"].to_csv(out / "forecast_timeline.csv", index=False)
            fc["alerts"].to_csv(out / "forecast_alerts.csv", index=False)
            joblib.dump(fc["model"], out / "forecaster_model.joblib")
        (out / "pipeline_summary.json").write_text(json.dumps(summary, indent=2, default=str))
        _write_summary_txt(out / "pipeline_summary.txt", summary)

    # attach in-memory artifacts (not serialized) for callers that want them
    summary["_master"] = master
    summary["_forecast"] = fc
    return summary


def _save_events(ev: pd.DataFrame, path: Path) -> None:
    if ev is None or ev.empty:
        return
    cols = [c for c in ("onset_time", "peak_time", "end_time", "peak_excess",
                        "peak_level", "duration_min") if c in ev.columns]
    ev[cols].to_csv(path, index=False)


def _write_summary_txt(path: Path, s: dict) -> None:
    lines = [
        "SURYA-KAVACH — PIPELINE RUN SUMMARY",
        "=" * 50,
        f"Samples processed : {s['samples']:,}",
        f"Time range        : {s['time_start']} -> {s['time_end']}",
        "",
        "NOWCASTING",
        "-" * 50,
        f"SXR detections    : {s['sxr_events']}",
        f"HXR detections    : {s['hxr_events']}",
        f"Master events     : {s['master_events']}  {s['status_counts']}",
    ]
    if s.get("forecast_ok"):
        lines += [
            "",
            "FORECASTING",
            "-" * 50,
            f"Validation AUC    : {s.get('val_auc'):.3f}",
            f"Test AUC          : {s.get('test_auc'):.3f}",
            f"Alert threshold   : {s.get('alert_threshold'):.2f}",
            f"True Positive Rate: {s.get('test_tpr')}",
            f"False Alarm Rate  : {s.get('test_far')}",
            f"Median lead (peak): {s.get('test_median_lead_peak_min')} min",
            f"Forecast alerts   : {s.get('n_forecast_alerts')}",
        ]
    path.write_text("\n".join(lines))
