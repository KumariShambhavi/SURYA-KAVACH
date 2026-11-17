"""
app.py — FastAPI application: REST endpoints, WebSocket live replay, static UI.

REST reads from the persistent DB and the latest run summary; ingestion runs in a
background thread so the UI never blocks. Optional API-key auth guards mutating
endpoints (set SOLARFLARE_API_KEY).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config, store
from ..ingest import get_validation, ingest, monitor_status, run_validation_sync
from .live import invalidate_cache, replay_socket

log = logging.getLogger("solarflare.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

MAX_LIMIT = 5000          # hard ceiling: `limit=-1` used to mean "no limit" in SQLite (full dump)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    # the scheduler runs only when explicitly enabled (CLI serve / Docker), never under tests
    if os.getenv("SOLARFLARE_RUN_SCHEDULER", "0") == "1":
        from ..scheduler import start as start_scheduler
        start_scheduler()
    yield
    try:
        from ..scheduler import stop as stop_scheduler
        stop_scheduler()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="SURYA-KAVACH API", version="1.0.0", lifespan=lifespan)

# CORS: the dashboard is served from THIS app (same origin), so "*" bought nothing and made
# POST /api/ingest a cross-origin "simple request" — any web page the operator visited could
# trigger a full recompute. Lock it to the configured origin(s).
_origins = [o.strip() for o in os.getenv(
    "SOLARFLARE_ALLOWED_ORIGINS",
    "http://127.0.0.1:8000,http://localhost:8000").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins,
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def require_key(x_api_key: str | None = Header(default=None)):
    if not config.API_KEY:
        return
    # constant-time compare (a plain != leaks the key length/prefix via timing)
    if not x_api_key or not secrets.compare_digest(x_api_key, config.API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# ----------------------------------------------------------------- REST
@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/monitor")
def monitor():
    return monitor_status()


@app.get("/api/summary")
def summary():
    mon = monitor_status()
    runs = store.get_runs(limit=1)
    metrics = {}
    if runs and runs[0].get("status") == "success":
        latest = store.get_runs(limit=1)[0]
        # pull forecast metrics from the stored summary_json
        with store.Session(store.engine()) as s:
            row = s.get(store.IngestRun, latest["id"])
            if row and row.summary_json:
                try:
                    metrics = json.loads(row.summary_json)
                except json.JSONDecodeError:
                    metrics = {}
    return {"monitor": mon, "metrics": metrics}


@app.get("/api/catalogue")
def catalogue(limit: int = Query(1000, ge=1, le=MAX_LIMIT), status: str | None = None):
    return store.get_flares(limit=limit, status=status)


@app.get("/api/alerts")
def alerts(limit: int = Query(500, ge=1, le=MAX_LIMIT)):
    return store.get_alerts(limit=limit)


@app.get("/api/stats/class-distribution")
def class_distribution():
    """A-X counts over the WHOLE catalogue (never derived from a page of results)."""
    return {"total": store.summary_counts()["total_flares"],
            "by_class": store.class_distribution()}


@app.get("/api/runs")
def runs(limit: int = Query(20, ge=1, le=200)):
    return store.get_runs(limit=limit)


def _alert_threshold() -> float:
    runs_ = store.get_runs(limit=1)
    if runs_ and runs_[0].get("status") == "success":
        with store.Session(store.engine()) as s:
            row = s.get(store.IngestRun, runs_[0]["id"])
            if row and row.summary_json:
                try:
                    return float(json.loads(row.summary_json).get("alert_threshold", 0.5))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
    return 0.5


@app.get("/api/forecast/timeline")
def forecast_timeline(limit: int = Query(1500, ge=1, le=MAX_LIMIT)):
    """Downsampled forecast-probability timeline + threshold + flare onsets (for markers)."""
    import pandas as pd
    p = config.OUTPUT_DIR / "forecast_timeline.csv"
    if not p.exists():
        return {"threshold": _alert_threshold(), "points": [], "flares": []}
    tl = pd.read_csv(p, parse_dates=["time"])
    step = max(1, len(tl) // max(1, limit))
    d = tl.iloc[::step]
    # Break the line at real data gaps. There are NO rows across a gap (June 2024 is a 30-day
    # hole), so without an explicit null the chart drew a straight line across it — asserting a
    # continuous forecast where no data exists.
    from .live import _insert_gap_breaks
    d = _insert_gap_breaks(d[["time", "proba"]].reset_index(drop=True), "time")
    points = [{"t": t.isoformat(), "proba": (None if pd.isna(v) else round(float(v), 4))}
              for t, v in zip(d["time"], d["proba"])]
    flares = [{"t": str(r["onset_time"]), "cls": r.get("flare_class")}
              for r in store.get_flares(limit=5000)
              if r.get("flare_class") and str(r["flare_class"])[:1] in ("C", "M", "X")]
    return {"threshold": _alert_threshold(), "points": points, "flares": flares}


@app.post("/api/ingest", dependencies=[Depends(require_key)])
def trigger_ingest(force: bool = False):
    # NOTE: the mutual exclusion now lives inside ingest() itself. Guarding only this endpoint
    # was useless: scheduler.py calls ingest() directly and bypassed the lock, so a scheduler
    # tick could run a second pipeline concurrently and corrupt the parquet/model/DB.
    def _run():
        res = ingest(force=force)
        if res.get("status") != "busy":
            invalidate_cache()          # refresh the live-replay data
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "force": force}


@app.get("/api/validation")
def validation():
    """Latest GOES cross-validation (completeness / purity)."""
    return get_validation()


@app.post("/api/validate", dependencies=[Depends(require_key)])
def trigger_validate():
    """Recompute GOES validation in the background from the current catalogue."""
    import pandas as pd
    master = pd.DataFrame(store.get_flares(limit=100000))

    def _run():
        try:
            run_validation_sync(master)
        except Exception as e:  # noqa: BLE001
            log.warning("validation failed: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


# ----------------------------------------------------------------- WebSocket
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    # The same-origin policy does NOT apply to WebSockets: without this check, any website the
    # operator visits could open ws://<host>/ws/live and read the whole replay stream (CSWSH).
    origin = ws.headers.get("origin")
    if origin and origin not in _origins:
        log.warning("rejected websocket from disallowed origin: %s", origin)
        await ws.close(code=1008)          # policy violation
        return
    if config.API_KEY:
        key = ws.query_params.get("api_key")
        if not key or not secrets.compare_digest(key, config.API_KEY):
            await ws.close(code=1008)
            return
    await ws.accept()
    try:
        await replay_socket(ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ws error: %s", e)


# ----------------------------------------------------------------- static UI
_DIST = config.ROOT_DIR / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
else:
    @app.get("/")
    def _root():
        return {"message": "SURYA-KAVACH API running. Build the frontend (frontend/dist) "
                           "for the dashboard. See /docs for the API."}
