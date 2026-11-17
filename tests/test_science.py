"""Science-core tests: schema, injected-flare detection, HXR-duration, leakage."""
import numpy as np
import pandas as pd
import pytest

from solarflare import config
from solarflare.schema import validate_light_curve, SchemaError, is_valid
from solarflare.synthetic import generate
from solarflare.nowcast import run_nowcast, classify_flare
from solarflare.forecast import build_features


@pytest.fixture(scope="module")
def synth():
    return generate(hours=6.0)


# ----------------------------------------------------------------- schema
def test_schema_accepts_valid(synth):
    df, _ = synth
    assert is_valid(df)
    validate_light_curve(df)  # must not raise


def test_schema_rejects_missing_column(synth):
    df, _ = synth
    with pytest.raises(SchemaError):
        validate_light_curve(df.drop(columns=[config.HXR_COL]))


def test_schema_rejects_naive_time(synth):
    df, _ = synth
    bad = df.copy()
    bad[config.TIME_COL] = bad[config.TIME_COL].dt.tz_localize(None)
    with pytest.raises(SchemaError):
        validate_light_curve(bad)


# ----------------------------------------------------------------- classification
def test_classify_flare():
    assert classify_flare(3e-6).startswith("C")
    assert classify_flare(2e-5).startswith("M")
    assert classify_flare(1.5e-4).startswith("X")
    assert classify_flare(0) == "Unclassified"


# ----------------------------------------------------------------- detection
def test_injected_flares_are_detected(synth):
    df, truth = synth
    res = run_nowcast(df)
    master = res["master"]
    peaks = pd.to_datetime(master["peak_time_sxr"])
    for f in truth:
        # a detected SXR peak must exist within 10 min of each injected peak
        dt = (peaks - f["peak_time"]).abs().dropna()
        assert (dt <= pd.Timedelta(minutes=10)).any(), f"missed flare at {f['peak_time']}"
    # ...and we must not invent a pile of extra ones. The old assertion was `>= len(truth)`,
    # which passes happily with 6,000 false positives — i.e. it pointed the WRONG WAY and could
    # never catch the real failure mode (over-detection).
    sxr_events = res["sxr"]
    assert len(sxr_events) <= len(truth) + 1, (
        f"over-detection: {len(sxr_events)} SXR events for {len(truth)} injected flares")


def test_quiet_sun_produces_no_flares():
    """THE regression test. A quiet Sun (Poisson noise only) must yield ZERO detections.

    This is the test that was missing: the shipped detector sat above its trigger threshold 62%
    of the time on real data, purely from photon noise, and the whole suite still passed because
    the synthetic fixture was ~20x too quiet to ever trip it.
    """
    df, truth = generate(hours=8.0, flares=[], seed=11)
    assert truth == []
    res = run_nowcast(df)
    assert len(res["sxr"]) == 0, (
        f"detector fired {len(res['sxr'])} times on a QUIET SUN (photon noise only)")
    assert len(res["master"]) == 0


def test_quiet_sun_trigger_duty_cycle_is_tiny():
    """The trigger must be above threshold only rarely on quiet Sun (< 1% of samples)."""
    import numpy as np
    from solarflare.preprocess import add_features
    df, _ = generate(hours=8.0, flares=[], seed=12)
    feat = add_features(df)
    excess = feat["sxr_excess"].to_numpy()
    noise = feat["sxr_noise"].to_numpy()
    above = np.nan_to_num(excess >= config.SXR_RISE_SIGMA * noise, nan=False)
    duty = float(above.mean())
    assert duty < 0.01, f"quiet-Sun trigger duty cycle is {duty:.1%} (must be < 1%)"


def test_preprocess_features_are_causal():
    """add_features must not see the future. (The old leakage test only checked build_features,
    and explicitly skipped the window where a trailing-window leak would appear.)"""
    import numpy as np
    from solarflare.preprocess import add_features
    df, _ = generate(hours=3.0, seed=5)
    base = add_features(df)
    tampered = df.copy()
    cut = len(df) // 2
    tampered.loc[tampered.index >= cut, config.SXR_COL] *= 1000.0
    tampered.loc[tampered.index >= cut, config.HXR_COL] += 5000.0
    after = add_features(tampered)
    for col in ("sxr_smooth", "sxr_bg", "sxr_excess", "sxr_noise", "hxr_smooth", "hxr_excess"):
        a = base[col].to_numpy()[:cut]
        b = after[col].to_numpy()[:cut]
        m = ~(np.isnan(a) & np.isnan(b))
        assert np.allclose(a[m], b[m], equal_nan=True), f"{col} leaked future data into the past"


def test_hxr_events_respect_max_duration(synth):
    df, _ = synth
    res = run_nowcast(df)
    hxr = res["hxr"]
    if len(hxr):
        cap = config.HXR_MAX_EVENT_DURATION_MIN
        assert hxr["duration_min"].max() <= cap + 1e-6, "HXR event exceeds physical max duration"


def test_neupert_lead_is_positive(synth):
    df, _ = synth
    res = run_nowcast(df)
    confirmed = res["master"][res["master"]["status"] == "confirmed"]
    # synthetic HXR leads SXR -> lead should be positive for confirmed events
    if len(confirmed):
        leads = confirmed["hxr_leads_sxr_minutes"].dropna()
        if len(leads):
            assert leads.median() > 0


# ----------------------------------------------------------------- leakage (causality)
def test_features_are_causal_no_future_leak(synth):
    """Mutating future samples must NOT change past feature values."""
    df, _ = synth
    f1 = build_features(df)
    cut = len(df) * 2 // 3
    tampered = df.copy()
    # blow up the future tail
    tampered.loc[tampered.index >= cut, config.SXR_COL] *= 1000.0
    tampered.loc[tampered.index >= cut, config.HXR_COL] += 5000.0
    f2 = build_features(tampered)

    # compare features strictly before the tampered region (minute-indexed)
    cut_time = df[config.TIME_COL].iloc[cut]
    past = f1.index < (cut_time - pd.Timedelta(minutes=config.CONTEXT_WINDOW_MIN))
    a = f1[past].to_numpy()
    b = f2.reindex(f1.index)[past].to_numpy()
    mask = ~np.isnan(a)
    assert np.allclose(a[mask], b[mask], equal_nan=True), "future data leaked into past features"


# ----------------------------------------------------------------- GOES validation
def test_validate_catalogue_completeness_purity():
    from solarflare.validate import validate_catalogue
    goes = pd.DataFrame({
        "peak_time": pd.to_datetime(["2024-03-01T02:00:00Z", "2024-03-01T05:00:00Z"]),
        "goes_class": ["C2.0", "M1.0"],
    })
    master = pd.DataFrame([
        {"flare_class": "C3.0", "peak_time_sxr": "2024-03-01 02:05:00+00:00",
         "onset_time": "2024-03-01 02:00:00+00:00"},   # matches the GOES C
        {"flare_class": "B5.0", "peak_time_sxr": "2024-03-01 09:00:00+00:00",
         "onset_time": "2024-03-01 08:55:00+00:00"},   # no GOES counterpart
    ])
    r = validate_catalogue(master, goes, tol_min=12)
    assert r["all"]["n_goes"] == 2 and r["all"]["n_ours"] == 2
    assert r["all"]["completeness"] == 0.5 and r["all"]["purity"] == 0.5
    # only the C-class one of ours counts at >=C; it matches a real GOES flare -> purity 1.0
    assert r["ge_C"]["purity"] == 1.0
    assert r["ge_C"]["completeness"] == 0.5      # caught 1 of 2 GOES >=C flares
