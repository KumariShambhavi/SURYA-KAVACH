"""
validate.py — scientific validation against an INDEPENDENT ground truth.

Cross-checks our nowcast master catalogue against the official NOAA/SWPC GOES
X-ray flare list (fetched from the Heliophysics Events Knowledgebase, HEK) for the
same UTC range, and reports:

  completeness  fraction of real GOES flares we detected      (recall)
  purity        fraction of our detections that are real      (precision)

This is the strongest "real, not a demo" evidence: an external reference the
pipeline never sees during detection. Results are cached to data/external so the
panel works offline after the first fetch; a user-supplied CSV is also honoured.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger("solarflare.validate")

HEK_URL = "https://www.lmsal.com/hek/her"
CLASS_ORDER = "ABCMX"
GOES_CACHE = config.EXTERNAL_DIR / "goes_flares.csv"
USER_CSV = config.EXTERNAL_DIR / "goes_flares_user.csv"   # optional manual reference


# --------------------------------------------------------------------------
def _http_get_json(url: str, params: dict) -> dict | None:
    """GET JSON over VERIFIED TLS.

    This fetches the project's scientific GROUND TRUTH. The previous version silently retried
    with ``verify=False`` on any TLS error, which means an on-path attacker (or a broken proxy)
    could serve an arbitrary flare list that we would then present as "independent NOAA/SWPC
    validation". We never downgrade. If a corporate CA is required, point SSL_CERT_FILE (or
    SOLARFLARE_CA_BUNDLE) at the bundle — explicitly, and visibly.
    """
    import httpx
    verify: object = os.getenv("SOLARFLARE_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or True
    try:
        with httpx.Client(verify=verify, timeout=60, follow_redirects=True) as cl:
            r = cl.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("HEK fetch failed (TLS is verified and never downgraded): %s", e)
        return None


def _class_rank(cls: str) -> int:
    return CLASS_ORDER.index(cls[0]) if cls and cls[0] in CLASS_ORDER else -1


MAX_HEK_PAGES = 200          # safety stop; HEK returns 100 events per page


def fetch_goes_flares(start: dt.datetime, end: dt.datetime,
                      chunk_days: int = 30) -> pd.DataFrame:
    """Fetch the SWPC GOES flare list from HEK for [start, end] -> DataFrame[peak_time, goes_class].

    HEK returns at most **100 events per page** and signals that more exist via
    ``overmax: True``. We MUST page through them all: querying a single page silently
    truncates the reference to the first 100 events per chunk — which drops most flares
    (including every X-class one) and makes every completeness/purity number computed
    against it invalid.
    """
    rows = []
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=chunk_days), end)
        page = 1
        while page <= MAX_HEK_PAGES:
            params = {
                "cosec": "2", "cmd": "search", "type": "column", "event_type": "FL",
                "event_starttime": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                "event_endtime": nxt.strftime("%Y-%m-%dT%H:%M:%S"),
                "event_coordsys": "helioprojective",
                "x1": "-1200", "x2": "1200", "y1": "-1200", "y2": "1200",
                "page": str(page),
            }
            data = _http_get_json(HEK_URL, params)
            if not data:
                break
            result = data.get("result", [])
            for e in result:
                # the official NOAA list is frm_name == "SWPC" (avoids HEK duplicates)
                if e.get("frm_name") != "SWPC":
                    continue
                cls = e.get("fl_goescls")
                pk = e.get("event_peaktime")
                if cls and pk:
                    rows.append({"peak_time": pk, "goes_class": cls,
                                 # start time lets the forecaster train on REAL flare onsets
                                 # instead of its own detector's output (circular labelling)
                                 "start_time": e.get("event_starttime") or pk})
            # HEK sets overmax=True while more pages remain
            if not result or not data.get("overmax", False):
                break
            page += 1
        cur = nxt

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["peak_time"] = pd.to_datetime(df["peak_time"], utc=True, errors="coerce")
    if "start_time" in df.columns:
        df["start_time"] = pd.to_datetime(df["start_time"], utc=True, errors="coerce")
        df["start_time"] = df["start_time"].fillna(df["peak_time"])
    df = df.dropna(subset=["peak_time"]).drop_duplicates(subset=["peak_time", "goes_class"])
    return df.sort_values("peak_time").reset_index(drop=True)


def goes_targets(start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """The official GOES flares as forecast TARGETS: [onset_time, peak_time_sxr, flare_class].

    Using these as the forecaster's labels (instead of our own detector's output) removes the
    circularity that made the reported metrics un-comparable to published flare forecasters.
    """
    g, _src = get_reference(start, end)
    if g is None or g.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "onset_time": g["start_time"] if "start_time" in g.columns else g["peak_time"],
        "peak_time_sxr": g["peak_time"],
        "flare_class": g["goes_class"],
    })
    return out.dropna(subset=["onset_time"]).sort_values("onset_time").reset_index(drop=True)


def get_reference(start: dt.datetime, end: dt.datetime) -> tuple[pd.DataFrame, str]:
    """Return (goes_df, source). Prefers a user CSV, then cache, then a live HEK fetch."""
    if USER_CSV.exists():
        df = pd.read_csv(USER_CSV)
        df["peak_time"] = pd.to_datetime(df["peak_time"], utc=True, errors="coerce")
        return df.dropna(subset=["peak_time"]), f"user CSV ({USER_CSV.name})"

    cached = pd.DataFrame()
    if GOES_CACHE.exists():
        cached = pd.read_csv(GOES_CACHE)
        cached["peak_time"] = pd.to_datetime(cached["peak_time"], utc=True, errors="coerce")
        cached = cached.dropna(subset=["peak_time"])
        # only trust the cache if it actually spans the requested range, otherwise
        # we'd compare against a partial reference and under-report purity
        if not cached.empty and cached["peak_time"].min() <= start and cached["peak_time"].max() >= end:
            in_range = cached[(cached["peak_time"] >= start) & (cached["peak_time"] <= end)]
            return in_range, "cache (HEK/SWPC)"

    fetched = fetch_goes_flares(start, end)
    if fetched.empty:
        return cached, "unavailable (no network / no cache)"
    # merge into cache
    config.EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)
    merged = (pd.concat([cached, fetched], ignore_index=True)
              .drop_duplicates(subset=["peak_time", "goes_class"]).sort_values("peak_time"))
    merged.to_csv(GOES_CACHE, index=False)
    return fetched, "HEK/SWPC (live)"


# --------------------------------------------------------------------------
def validate_catalogue(master: pd.DataFrame, goes: pd.DataFrame,
                       tol_min: int = 12) -> dict:
    """Completeness + purity of ``master`` against the ``goes`` reference list."""
    tol = pd.Timedelta(minutes=tol_min)

    def our_peaks(min_rank: int = -1):
        out = []
        for _, r in master.iterrows():
            cls = str(r.get("flare_class") or "")
            if _class_rank(cls) < min_rank:
                continue
            t = r.get("peak_time_sxr") or r.get("onset_time")
            if pd.notna(t):
                out.append(pd.Timestamp(t))
        return pd.DatetimeIndex(out)

    def goes_peaks(min_rank: int = -1):
        g = goes[goes["goes_class"].map(lambda c: _class_rank(str(c)) >= min_rank)]
        return pd.DatetimeIndex(pd.to_datetime(g["peak_time"], utc=True))

    def match_one_to_one(a: pd.DatetimeIndex, b: pd.DatetimeIndex) -> int:
        """Count EXCLUSIVE 1:1 pairs between ``a`` and ``b`` within ``tol``.

        The previous implementation let MANY elements of ``a`` match the SAME element of ``b``,
        so a flare we fragmented into 3 detections scored 3 hits — inflating purity by exactly
        the fragmentation the detector produces. We now greedily pair closest-first and REMOVE
        each matched element, which is what "completeness" and "purity" actually mean.
        """
        if len(a) == 0 or len(b) == 0:
            return 0
        av = np.sort(a.values.astype("datetime64[s]").astype("int64"))
        bv = np.sort(b.values.astype("datetime64[s]").astype("int64"))
        tol_s = int(tol.total_seconds())

        # all candidate pairs within tolerance, closest first
        pairs = []
        for i, t in enumerate(av):
            lo = np.searchsorted(bv, t - tol_s, side="left")
            hi = np.searchsorted(bv, t + tol_s, side="right")
            for j in range(lo, hi):
                pairs.append((abs(int(bv[j]) - int(t)), i, j))
        pairs.sort()

        used_a, used_b, hits = set(), set(), 0
        for _d, i, j in pairs:
            if i in used_a or j in used_b:
                continue
            used_a.add(i)
            used_b.add(j)
            hits += 1
        return hits

    result = {"tolerance_min": tol_min}
    for label, min_rank in (("all", -1), ("ge_C", CLASS_ORDER.index("C")),
                            ("ge_M", CLASS_ORDER.index("M"))):
        op, gp = our_peaks(min_rank), goes_peaks(min_rank)
        # one exclusive pairing serves both metrics: a matched pair is simultaneously
        # "a GOES flare we found" (completeness) and "a detection that is real" (purity)
        hits = match_one_to_one(gp, op)
        result[label] = {
            "n_goes": len(gp), "n_ours": len(op), "matched": hits,
            "completeness": round(hits / len(gp), 3) if len(gp) else None,
            "purity": round(hits / len(op), 3) if len(op) else None,
        }
    return result


def run_validation(master: pd.DataFrame) -> dict:
    """Fetch the reference for the catalogue's time span and validate. Best-effort."""
    if master is None or master.empty:
        return {"ok": False, "reason": "empty catalogue"}
    times = pd.to_datetime(master["onset_time"], utc=True, errors="coerce").dropna()
    if times.empty:
        return {"ok": False, "reason": "no valid times"}
    start = times.min().to_pydatetime() - dt.timedelta(hours=1)
    end = times.max().to_pydatetime() + dt.timedelta(hours=1)
    goes, source = get_reference(start, end)
    if goes is None or goes.empty:
        return {"ok": False, "reason": "no GOES reference available", "source": source}
    metrics = validate_catalogue(master, goes)
    return {"ok": True, "source": source,
            "range": f"{start.date()} .. {end.date()}", **metrics}
