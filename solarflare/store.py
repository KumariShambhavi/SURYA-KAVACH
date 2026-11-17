"""
store.py — SQLite persistence (SQLAlchemy 2.0) with fingerprint deduplication.

Tables: flares, forecast_alerts, ingest_runs, app_state.
Re-ingesting identical data updates ``last_seen`` instead of inserting, so the
master catalogue accumulates across windows with zero duplicates. SQLite runs in
WAL mode (concurrent reads during writes); the engine is Postgres-ready (only the
URL changes).
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Iterable

import pandas as pd
from sqlalchemy import (Column, DateTime, Float, Integer, String, Text,
                        create_engine, event, func, select)
from sqlalchemy.orm import DeclarativeBase, Session

from . import config


class Base(DeclarativeBase):
    pass


class Flare(Base):
    __tablename__ = "flares"
    id = Column(Integer, primary_key=True)
    event_id = Column(String, unique=True)
    fingerprint = Column(String, unique=True, index=True)
    status = Column(String, index=True)
    confidence = Column(String)
    onset_time = Column(String)
    peak_time_sxr = Column(String)
    peak_time_hxr = Column(String)
    end_time = Column(String)
    flare_class = Column(String)
    peak_sxr_flux_w_m2 = Column(Float)
    peak_hxr_counts_s = Column(Float)
    hxr_leads_sxr_minutes = Column(Float)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)


class ForecastAlert(Base):
    __tablename__ = "forecast_alerts"
    id = Column(Integer, primary_key=True)
    alert_id = Column(String, unique=True)
    fingerprint = Column(String, unique=True, index=True)
    start_time = Column(String)
    end_time = Column(String)
    peak_probability = Column(Float)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)


class IngestRun(Base):
    __tablename__ = "ingest_runs"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    status = Column(String)
    error_message = Column(Text)
    samples = Column(Integer)
    time_range = Column(String)
    master_events = Column(Integer)
    summary_json = Column(Text)


class AppState(Base):
    __tablename__ = "app_state"
    key = Column(String, primary_key=True)
    value = Column(Text)


_engine = None


def engine():
    global _engine
    if _engine is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{config.DB_PATH}", future=True,
                                connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _wal(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        Base.metadata.create_all(_engine)
    return _engine


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fp(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _minute(ts) -> str:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)) or pd.isna(ts):
        return ""
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------------
def init_db() -> None:
    engine()


def flare_fingerprint(r) -> str:
    return _fp(r["status"], _minute(r["onset_time"]), _minute(r.get("peak_time_sxr")),
               _minute(r.get("peak_time_hxr")), r.get("flare_class"))


def existing_fingerprints() -> set[str]:
    with Session(engine()) as s:
        return set(s.scalars(select(Flare.fingerprint)).all())


def upsert_flares(master: pd.DataFrame) -> tuple[int, int]:
    if master is None or master.empty:
        return (0, 0)
    inserted = updated = 0
    with Session(engine()) as s:
        for _, r in master.iterrows():
            fp = flare_fingerprint(r)
            existing = s.scalar(select(Flare).where(Flare.fingerprint == fp))
            if existing:
                existing.last_seen = _now()
                updated += 1
                continue
            s.add(Flare(
                event_id=f"FLR_{fp}", fingerprint=fp, status=r["status"],
                confidence=r.get("confidence"),
                onset_time=_str(r.get("onset_time")),
                peak_time_sxr=_str(r.get("peak_time_sxr")),
                peak_time_hxr=_str(r.get("peak_time_hxr")),
                end_time=_str(r.get("end_time")),
                flare_class=r.get("flare_class"),
                peak_sxr_flux_w_m2=_f(r.get("peak_sxr_flux_w_m2")),
                peak_hxr_counts_s=_f(r.get("peak_hxr_counts_s")),
                hxr_leads_sxr_minutes=_f(r.get("hxr_leads_sxr_minutes")),
                first_seen=_now(), last_seen=_now()))
            inserted += 1
        s.commit()
    return (inserted, updated)


def prune_flares(valid: set[str]) -> int:
    """Delete flares whose fingerprint is NOT in ``valid``. Returns the number removed.

    ``run_pipeline`` is a FULL recompute over everything in data/raw, so the master catalogue it
    produces IS the complete, authoritative catalogue. Without pruning, upsert_flares was
    append-only: every detector/calibration change produced new fingerprints (different peak
    times/classes), so the old rows survived forever and the DB became the UNION of every
    detector version ever run. Observed in practice: 18,920 rows in the DB for a 3,350-event
    catalogue, which dragged the reported >=M purity from 0.712 down to 0.228.
    """
    if not valid:
        return 0
    with Session(engine()) as s:
        stale = s.scalars(select(Flare).where(Flare.fingerprint.notin_(valid))).all()
        for row in stale:
            s.delete(row)
        s.commit()
        return len(stale)


def prune_alerts(valid: set[str]) -> int:
    """Delete forecast alerts whose fingerprint is NOT in ``valid`` (same reason as prune_flares)."""
    if not valid:
        return 0
    with Session(engine()) as s:
        stale = s.scalars(select(ForecastAlert).where(ForecastAlert.fingerprint.notin_(valid))).all()
        for row in stale:
            s.delete(row)
        s.commit()
        return len(stale)


def upsert_alerts(alerts: pd.DataFrame) -> tuple[int, int]:
    if alerts is None or alerts.empty:
        return (0, 0)
    inserted = updated = 0
    with Session(engine()) as s:
        for _, r in alerts.iterrows():
            fp = _fp(_minute(r["start_time"]), _minute(r["end_time"]))
            existing = s.scalar(select(ForecastAlert).where(ForecastAlert.fingerprint == fp))
            if existing:
                existing.last_seen = _now()
                existing.peak_probability = _f(r.get("peak_probability"))
                updated += 1
                continue
            s.add(ForecastAlert(
                alert_id=f"ALT_{fp}", fingerprint=fp,
                start_time=_str(r["start_time"]), end_time=_str(r["end_time"]),
                peak_probability=_f(r.get("peak_probability")),
                first_seen=_now(), last_seen=_now()))
            inserted += 1
        s.commit()
    return (inserted, updated)


def record_run(started: dt.datetime, status: str, summary: dict | None = None,
               error: str | None = None) -> None:
    import json
    with Session(engine()) as s:
        s.add(IngestRun(
            started_at=started, finished_at=_now(), status=status, error_message=error,
            samples=int(summary.get("samples", 0)) if summary else None,
            time_range=(f"{summary.get('time_start')} -> {summary.get('time_end')}"
                        if summary else None),
            master_events=int(summary.get("master_events", 0)) if summary else None,
            summary_json=json.dumps(summary, default=str) if summary else None))
        s.commit()


def set_state(key: str, value: str) -> None:
    with Session(engine()) as s:
        st = s.get(AppState, key)
        if st:
            st.value = value
        else:
            s.add(AppState(key=key, value=value))
        s.commit()


def get_state(key: str, default: str | None = None) -> str | None:
    with Session(engine()) as s:
        st = s.get(AppState, key)
        return st.value if st else default


# --------------------------------------------------------------------------
# Read helpers (return plain dicts for the API)
# --------------------------------------------------------------------------
def get_flares(limit: int = 1000, status: str | None = None) -> list[dict]:
    with Session(engine()) as s:
        q = select(Flare).order_by(Flare.onset_time.desc())
        if status:
            q = q.where(Flare.status == status)
        rows = s.scalars(q.limit(limit)).all()
        return [_flare_dict(r) for r in rows]


def get_alerts(limit: int = 500) -> list[dict]:
    with Session(engine()) as s:
        rows = s.scalars(select(ForecastAlert).order_by(
            ForecastAlert.start_time.desc()).limit(limit)).all()
        return [{"alert_id": r.alert_id, "start_time": r.start_time, "end_time": r.end_time,
                 "peak_probability": r.peak_probability} for r in rows]


def get_runs(limit: int = 20) -> list[dict]:
    with Session(engine()) as s:
        rows = s.scalars(select(IngestRun).order_by(IngestRun.id.desc()).limit(limit)).all()
        return [{"id": r.id, "started_at": str(r.started_at), "finished_at": str(r.finished_at),
                 "status": r.status, "samples": r.samples, "time_range": r.time_range,
                 "master_events": r.master_events, "error": r.error_message} for r in rows]


def summary_counts() -> dict:
    with Session(engine()) as s:
        total = s.scalar(select(func.count(Flare.id))) or 0
        by_status = dict(s.execute(
            select(Flare.status, func.count(Flare.id)).group_by(Flare.status)).all())
        alerts = s.scalar(select(func.count(ForecastAlert.id))) or 0
        return {"total_flares": total, "by_status": by_status, "total_alerts": alerts}


def class_distribution() -> dict:
    """A–X counts computed SERVER-SIDE over the whole table (GROUP BY).

    The dashboard used to build this chart from whatever page of the catalogue it happened to
    have fetched (`limit=2000`), so with 3,328 flares it showed ~60% of reality — while the KPI
    beside it displayed the true COUNT(*). A distribution must never be derived from a page.
    """
    with Session(engine()) as s:
        rows = s.execute(select(Flare.flare_class, func.count(Flare.id))
                         .group_by(Flare.flare_class)).all()
    out: dict[str, int] = {}
    for cls, n in rows:
        letter = (str(cls)[:1] or "U")
        if letter not in "ABCMX":
            letter = "U"                 # unclassified (HXR-only)
        out[letter] = out.get(letter, 0) + int(n)
    return out


def _flare_dict(r: Flare) -> dict:
    return {c.name: getattr(r, c.name) for c in Flare.__table__.columns
            if c.name not in ("first_seen", "last_seen")}


def _str(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v) else str(v)


def _f(v):
    try:
        return None if v is None or pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return None
