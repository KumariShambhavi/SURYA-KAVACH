"""Persistence (dedup), API smoke, and auth tests."""
import fastapi
import pandas as pd
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Point the store at a throwaway DB (no module reload needed)."""
    from solarflare import config, store
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(store, "_engine", None)
    store.init_db()
    return store


def _master():
    return pd.DataFrame([
        {"event_id": "FLR_0001", "status": "confirmed", "confidence": "high",
         "onset_time": "2024-03-01 01:00:00+00:00", "peak_time_sxr": "2024-03-01 01:10:00+00:00",
         "peak_time_hxr": "2024-03-01 01:08:00+00:00", "end_time": "2024-03-01 01:30:00+00:00",
         "flare_class": "C3.0", "peak_sxr_flux_w_m2": 3e-6, "peak_hxr_counts_s": 400.0,
         "hxr_leads_sxr_minutes": 2.0},
        {"event_id": "FLR_0002", "status": "sxr_only", "confidence": "medium",
         "onset_time": "2024-03-01 05:00:00+00:00", "peak_time_sxr": "2024-03-01 05:20:00+00:00",
         "peak_time_hxr": None, "end_time": "2024-03-01 05:40:00+00:00",
         "flare_class": "B5.0", "peak_sxr_flux_w_m2": 5e-7, "peak_hxr_counts_s": None,
         "hxr_leads_sxr_minutes": None},
    ])


def test_dedup_reingest_adds_zero(isolated_store):
    store = isolated_store
    ins1, upd1 = store.upsert_flares(_master())
    assert (ins1, upd1) == (2, 0)
    ins2, upd2 = store.upsert_flares(_master())
    assert (ins2, upd2) == (0, 2)               # nothing new; all updated
    assert store.summary_counts()["total_flares"] == 2


def test_catalogue_filter_by_status(isolated_store):
    store = isolated_store
    store.upsert_flares(_master())
    conf = store.get_flares(status="confirmed")
    assert len(conf) == 1 and conf[0]["status"] == "confirmed"


def test_api_endpoints_smoke(isolated_store):
    store = isolated_store
    store.upsert_flares(_master())
    from solarflare.api.app import app
    c = TestClient(app)
    assert c.get("/api/health").json()["status"] == "ok"
    assert c.get("/api/monitor").status_code == 200
    cat = c.get("/api/catalogue").json()
    assert isinstance(cat, list)
    for row in cat:
        assert {"event_id", "status", "flare_class", "onset_time"} <= set(row)


def test_auth_require_key(monkeypatch):
    from solarflare import config
    from solarflare.api.app import require_key
    # no key configured -> open
    monkeypatch.setattr(config, "API_KEY", "")
    assert require_key(None) is None
    # key configured -> wrong/missing rejected, correct accepted
    monkeypatch.setattr(config, "API_KEY", "secret123")
    with pytest.raises(fastapi.HTTPException):
        require_key(None)
    with pytest.raises(fastapi.HTTPException):
        require_key("wrong")
    assert require_key("secret123") is None
