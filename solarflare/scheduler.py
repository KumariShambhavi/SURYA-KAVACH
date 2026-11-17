"""
scheduler.py — background daemon for near-real-time operation.

Every CHECK_INTERVAL seconds it (a) auto-converts any new PRADAN zips dropped into
data/incoming/, and (b) re-ingests when the raw data changed. This is what makes
the system "live": new Aditya-L1 files appear -> within one interval the catalogue,
forecast and alerts update and the dashboard refreshes. (Aditya-L1 data has downlink
latency, so this is near-real-time, bounded by ISSDC availability.)
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import threading

from . import config
from .ingest import has_new_raw_data, ingest

log = logging.getLogger("solarflare.scheduler")

_thread: threading.Thread | None = None
_stop = threading.Event()


def auto_convert_incoming() -> int:
    """Convert any *.zip in data/incoming/ then move them to _converted/. Returns #converted."""
    inc = config.INCOMING_DIR
    zips = glob.glob(str(inc / "*.zip"))
    if not zips:
        return 0
    from .io import convert_all
    log.info("auto-converting %d new PRADAN zip(s) from incoming/", len(zips))
    convert_all(source_dir=inc)
    done = inc / "_converted"
    done.mkdir(parents=True, exist_ok=True)
    for z in zips:
        try:
            shutil.move(z, done / os.path.basename(z))
        except OSError:
            pass
    return len(zips)


def _loop() -> None:
    log.info("scheduler running (interval %ss, auto_ingest=%s)",
             config.CHECK_INTERVAL_S, config.AUTO_INGEST)
    while not _stop.is_set():
        try:
            if config.AUTO_INGEST:
                auto_convert_incoming()
                if has_new_raw_data():
                    log.info("new raw data detected -> ingesting")
                    ingest()
                    try:
                        from .api.live import invalidate_cache
                        invalidate_cache()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001 - a bad tick must not kill the daemon
            log.warning("scheduler tick error: %s", e)
        _stop.wait(config.CHECK_INTERVAL_S)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="solarflare-scheduler")
    _thread.start()


def stop() -> None:
    _stop.set()
