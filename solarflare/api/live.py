"""
live.py — WebSocket live-replay engine.

Real Aditya-L1 data is not streamed to us in real time, so the dashboard replays
the processed light curve in accelerated time through the SAME representation the
true tail-ingest path would use: an ``init`` message (metadata + thresholds +
events) followed by ``frame`` batches (timestamps + SXR/HXR flux + forecast
probability + any flare events firing in that batch).

Client control messages (JSON): {"cmd":"pause"|"resume"|"restart"}, {"cmd":"speed","value":8}.
"""
from __future__ import annotations

import asyncio
import json
import math

import numpy as np
import pandas as pd

from .. import config, store

N_DISPLAY = 4000          # points shown across the full timeline
BATCH = 25                # points pushed per tick  -> N_DISPLAY/BATCH = 160 ticks per full pass
# Seconds between ticks (divided by speed). At the old 0.18 s a full 6-month replay finished in
# 160 x 0.18/8 = 3.6 s and then wiped the chart and restarted — a strobing loop nobody could read.
# At 1.0 s a full pass takes ~160 s at 1x and ~20 s at the default 8x, which is actually watchable.
TICK_S = 1.0

_cache: dict | None = None


def _clean(v):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)


GAP_BREAK_FACTOR = 5     # a step >5x the normal cadence is a real hole, not just the next sample
GAP_BREAK_MIN_S = 900    # ...and it must be at least 15 real minutes


def _insert_gap_breaks(d: pd.DataFrame, time_col: str,
                       factor: float = GAP_BREAK_FACTOR) -> pd.DataFrame:
    """Insert a NaN-valued row inside every genuine data gap.

    A chart can only break a line where it sees a NaN. Our light curve has NO ROWS across a gap
    (June 2024 is a 30-day hole), so the plot silently interpolated across it — drawing a straight
    line through a month with no data and presenting it as signal.

    The threshold must be RELATIVE to the series' own cadence: these frames are heavily
    downsampled (15.4 M rows -> a few thousand), so neighbouring points are already hours apart.
    A fixed 15-minute rule fired between *every* pair and shattered the chart. We break only where
    a step is ``factor`` x the median spacing (and at least GAP_BREAK_MIN_S).
    """
    if len(d) < 3:
        return d
    t = pd.DatetimeIndex(d[time_col])
    secs = t.values.astype("datetime64[s]").astype("int64")
    dt = np.diff(secs)
    med = float(np.median(dt))
    if not np.isfinite(med) or med <= 0:
        return d
    thresh = max(factor * med, GAP_BREAK_MIN_S)
    gaps = np.flatnonzero(dt > thresh)
    if gaps.size == 0:
        return d
    value_cols = [c for c in d.columns if c != time_col]
    filler = pd.DataFrame({time_col: [t[i] + pd.Timedelta(seconds=med) for i in gaps]})
    for c in value_cols:
        filler[c] = np.nan
    out = pd.concat([d, filler], ignore_index=True).sort_values(time_col)
    return out.reset_index(drop=True)


def _load_replay_data() -> dict:
    """Load + downsample the light curve, forecast timeline and events (cached)."""
    global _cache
    if _cache is not None:
        return _cache

    if not config.PROCESSED_LIGHT_CURVE.exists():
        _cache = {"empty": True}
        return _cache

    df = pd.read_parquet(config.PROCESSED_LIGHT_CURVE)
    step = max(1, len(df) // N_DISPLAY)
    d = df.iloc[::step].reset_index(drop=True)

    # Insert an explicit NaN break at every real data gap. The light curve simply has NO ROWS
    # where data is missing (e.g. all of June 2024 — a 30-day hole), so Plotly's connectgaps=false
    # had nothing to break on and drew a straight line straight across a month with no data,
    # making the chart claim a continuous signal that does not exist.
    d = _insert_gap_breaks(d, config.TIME_COL)

    # forecast probability aligned to the display times
    proba = pd.Series(np.nan, index=d.index)
    tl_path = config.OUTPUT_DIR / "forecast_timeline.csv"
    if tl_path.exists():
        tl = pd.read_csv(tl_path, parse_dates=["time"])
        if len(tl):
            merged = pd.merge_asof(
                d[[config.TIME_COL]].rename(columns={config.TIME_COL: "time"}),
                tl[["time", "proba"]].sort_values("time"),
                on="time", direction="nearest",
                tolerance=pd.Timedelta(minutes=2))
            proba = merged["proba"]

    times = pd.DatetimeIndex(d[config.TIME_COL])
    threshold = 0.5
    runs = store.get_runs(limit=1)
    if runs and runs[0].get("status") == "success":
        with store.Session(store.engine()) as s:
            row = s.get(store.IngestRun, runs[0]["id"])
            if row and row.summary_json:
                try:
                    threshold = float(json.loads(row.summary_json).get("alert_threshold", 0.5))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

    # events: map each flare to the nearest display index
    events = []
    for f in store.get_flares(limit=5000):
        ts = f.get("peak_time_sxr") or f.get("onset_time")
        if not ts:
            continue
        t = pd.Timestamp(ts)
        idx = int(times.searchsorted(t))
        if 0 <= idx < len(times):
            events.append({"i": idx, "class": f.get("flare_class"), "status": f.get("status")})

    _cache = {
        "empty": False,
        "times": [t.isoformat() for t in times],
        "sxr": [_clean(v) for v in d[config.SXR_COL]],
        "hxr": [_clean(v) for v in d[config.HXR_COL]],
        "proba": [_clean(v) for v in proba],
        "events": events,
        "threshold": threshold,
        "n": len(d),
    }
    return _cache


def invalidate_cache() -> None:
    global _cache
    _cache = None


async def replay_socket(ws) -> None:
    data = _load_replay_data()
    if data.get("empty"):
        await ws.send_json({"type": "init", "empty": True,
                            "message": "no processed data yet — run an ingest first"})
        return

    state = {"speed": 8.0, "paused": False, "restart": False, "closed": False}

    async def reader():
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                cmd = msg.get("cmd")
                if cmd == "pause":
                    state["paused"] = True
                elif cmd == "resume":
                    state["paused"] = False
                elif cmd == "restart":
                    state["restart"] = True
                elif cmd == "speed":
                    state["speed"] = max(1.0, min(64.0, float(msg.get("value", 8))))
        except Exception:  # noqa: BLE001 - client closed
            state["closed"] = True

    reader_task = asyncio.create_task(reader())

    await ws.send_json({
        "type": "init", "empty": False, "n": data["n"], "threshold": data["threshold"],
        "time_start": data["times"][0], "time_end": data["times"][-1],
        "n_events": len(data["events"]),
    })

    ev_by_index: dict[int, list] = {}
    for e in data["events"]:
        ev_by_index.setdefault(e["i"], []).append(e)

    i = 0
    try:
        while not state["closed"]:
            if state["restart"]:
                i = 0
                state["restart"] = False
                await ws.send_json({"type": "reset"})
            if state["paused"]:
                await asyncio.sleep(0.1)
                continue
            j = min(i + BATCH, data["n"])
            frame_events = []
            for k in range(i, j):
                frame_events.extend(ev_by_index.get(k, []))
            await ws.send_json({
                "type": "frame",
                "t": data["times"][i:j],
                "sxr": data["sxr"][i:j],
                "hxr": data["hxr"][i:j],
                "proba": data["proba"][i:j],
                "events": frame_events,
            })
            i = j
            if i >= data["n"]:
                await asyncio.sleep(1.5)
                i = 0
                await ws.send_json({"type": "reset"})
            await asyncio.sleep(TICK_S / state["speed"])
    finally:
        reader_task.cancel()
