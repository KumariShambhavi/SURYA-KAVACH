"""
ingest.py — run the pipeline and persist results to the database.

This is the operational entry point: it runs the science pipeline, upserts the
master catalogue + forecast alerts (deduplicated), and records the run in history.
A fingerprint of the raw data lets the scheduler skip work when nothing changed.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import logging
import math
import os
import threading

from . import config, store
from .pipeline import run_pipeline

log = logging.getLogger("solarflare.ingest")


def raw_fingerprint() -> str:
    """Hash of raw-CSV names + sizes + mtimes; changes when new data lands."""
    parts = []
    for d in (config.SOLEXS_RAW_DIR, config.HEL1OS_RAW_DIR):
        for p in sorted(glob.glob(str(d / "*.csv"))):
            st = os.stat(p)
            parts.append(f"{os.path.basename(p)}:{st.st_size}:{int(st.st_mtime)}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def has_new_raw_data() -> bool:
    return store.get_state("raw_fingerprint") != raw_fingerprint()


_INGEST_LOCK = threading.Lock()


def _json_safe(o):
    """Replace non-finite floats with None.

    ``evaluate`` legitimately returns NaN (e.g. TPR when the test window has no flares), and
    ``json.dumps`` happily writes a bare ``NaN`` literal — which Starlette then refuses to
    serialise (``allow_nan=False``), turning /api/summary into a hard 500 that the dashboard
    silently swallows. Sanitise at the boundary instead.
    """
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


def ingest(force: bool = False) -> dict:
    """Run the pipeline and upsert into the DB. Returns a status dict.

    The lock lives HERE, not on the API endpoint: scheduler.py calls this function directly, so
    an endpoint-only lock let a scheduler tick run a second pipeline concurrently — two writers
    on the same parquet / model / SQLite file.
    """
    if not _INGEST_LOCK.acquire(blocking=False):
        return {"status": "busy", "reason": "an ingest is already running"}
    try:
        return _ingest_locked(force)
    finally:
        _INGEST_LOCK.release()


def _ingest_locked(force: bool = False) -> dict:
    store.init_db()
    started = dt.datetime.now(dt.timezone.utc)
    if not force and not has_new_raw_data():
        return {"status": "skipped", "reason": "no new raw data"}

    store.set_state("status", "INGESTING")
    try:
        summary = run_pipeline(save=True)
        master = summary.pop("_master")
        before = store.existing_fingerprints()
        ins_f, upd_f = store.upsert_flares(master)
        # notify on genuinely-new significant flares (best-effort, never fatal)
        if ins_f and master is not None and len(master):
            new_rows = [r for _, r in master.iterrows()
                        if store.flare_fingerprint(r) not in before]
            try:
                from .notify import notify_new_flares
                notify_new_flares([dict(r) for r in new_rows], min_class="M")
            except Exception:  # noqa: BLE001
                pass
        fc = summary.pop("_forecast", {})
        ins_a = upd_a = 0
        if fc.get("ok"):
            ins_a, upd_a = store.upsert_alerts(fc["alerts"])

        # The pipeline is a FULL recompute, so the DB must MIRROR the new catalogue — not
        # accumulate. Prune anything the current detector no longer produces, otherwise every
        # detector/calibration change leaves its old events behind forever (they get new
        # fingerprints, so upsert never touches them) and the catalogue silently becomes the
        # union of every version ever run.
        pruned_f = pruned_a = 0
        if master is not None and len(master):
            valid_f = {store.flare_fingerprint(r) for _, r in master.iterrows()}
            pruned_f = store.prune_flares(valid_f)
        if fc.get("ok") and fc["alerts"] is not None and len(fc["alerts"]):
            valid_a = {store._fp(store._minute(r["start_time"]), store._minute(r["end_time"]))
                       for _, r in fc["alerts"].iterrows()}
            pruned_a = store.prune_alerts(valid_a)
        if pruned_f or pruned_a:
            log.info("pruned %d stale flares and %d stale alerts (superseded by this run)",
                     pruned_f, pruned_a)

        store.record_run(started, "success", _json_safe(summary))
        store.set_state("raw_fingerprint", raw_fingerprint())
        store.set_state("status", "MONITORING")
        store.set_state("last_ingest", dt.datetime.now(dt.timezone.utc).isoformat())
        # refresh GOES validation in the background (network/cache; never blocks ingest)
        run_validation_async(master)
        return {"status": "success", "flares_inserted": ins_f, "flares_updated": upd_f,
                "alerts_inserted": ins_a, "alerts_updated": upd_a, "summary": summary}
    except Exception as e:  # noqa: BLE001 - record and re-surface
        store.record_run(started, "error", error=str(e))
        store.set_state("status", "MONITORING")
        return {"status": "error", "error": str(e)}


def run_validation_async(master) -> None:
    """Run GOES validation in a daemon thread and persist the result to app_state."""
    def _work():
        try:
            from .validate import run_validation
            res = run_validation(master)
            store.set_state("validation", json.dumps(res, default=str))
        except Exception as e:  # noqa: BLE001
            store.set_state("validation", json.dumps({"ok": False, "reason": str(e)}))
    threading.Thread(target=_work, daemon=True).start()


def run_validation_sync(master) -> dict:
    """Run GOES validation now (blocking) and persist; returns the result."""
    from .validate import run_validation
    res = run_validation(master)
    store.set_state("validation", json.dumps(res, default=str))
    return res


def get_validation() -> dict:
    raw = store.get_state("validation")
    if not raw:
        return {"ok": False, "reason": "not run yet"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "parse error"}


def monitor_status() -> dict:
    store.init_db()
    counts = store.summary_counts()
    return {
        "status": store.get_state("status", "IDLE"),
        "last_ingest": store.get_state("last_ingest"),
        "auto_ingest": config.AUTO_INGEST,
        "has_new_data": has_new_raw_data(),
        **counts,
    }
