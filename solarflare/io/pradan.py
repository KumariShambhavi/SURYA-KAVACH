"""
pradan.py — turn raw ISSDC PRADAN ZIPs into clean CSVs the loader understands.

A PRADAN download is a ZIP with native instrument products in nested folders:

  SoLEXS  AL1_SLX_*.zip -> .../SDDx/AL1_SOLEXS_<date>_SDDx_L1.lc.gz
                          (gzipped FITS; HDU with TIME=Unix seconds, COUNTS)
  HEL1OS  HLS_*.zip      -> .../czt/lightcurve_czt1.fits
                          (multi-HDU FITS, one HDU per energy band; we take the
                           widest/total band: ISOT timestamp + CTR counts/sec)

Output (the loader's input contract):
  data/raw/solexs/<archive>.csv   columns: ISOT, COUNTS
  data/raw/hel1os/<archive>.csv   columns: ISOT, CTR

No data loss beyond intentional standardization: every native sample is kept;
the loader re-grids, segments and flags gaps later.
"""
from __future__ import annotations

import gzip
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config

# Hard X-ray detector lightcurve preference. CZT1 (~18-160 keV) is the broad
# band the project validated on; CZT2 is the fallback.
HEL1OS_LIGHTCURVE_PREFERENCE = ["lightcurve_czt1.fits", "lightcurve_czt2.fits"]
# EXTNAMEs look like "CZT1_LC_BAND_20.00KEV_TO_40.00KEV" — note the UNDERSCORES. The original
# pattern only allowed whitespace ([\s]*), so it never matched and every band scored equal; the
# band actually used was whichever HDU happened to come first. It landed on 20-40 keV (which is
# in fact the best band, SNR 5.2) purely by luck. Allow underscores so the choice is DELIBERATE
# and cannot silently change if ISSDC reorders the HDUs.
_KEV_RANGE_RE = re.compile(r"([\d.]+)\s*KEV[\s_]*TO[\s_]*([\d.]+)\s*KEV", re.IGNORECASE)


# ----------------------------------------------------------------- SoLEXS
def read_solexs_lc(path: Path) -> pd.DataFrame:
    """Read one SoLEXS .lc/.lc.gz (FITS) -> DataFrame[ISOT, COUNTS]."""
    from astropy.io import fits

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        with fits.open(fh, memmap=False) as hdul:
            table = _first_table_with(hdul, {"TIME", "COUNTS"})
            if table is None:
                raise ValueError(f"no TIME/COUNTS table HDU in {path.name}")
            time = np.asarray(table.data["TIME"], dtype="float64")
            counts = np.asarray(table.data["COUNTS"], dtype="float64")

    # SoLEXS TIME is seconds from MJDREF 40587 (= 1970-01-01) = Unix epoch.
    isot = pd.to_datetime(time, unit="s", origin="unix", utc=True)
    return pd.DataFrame({"ISOT": isot.strftime("%Y-%m-%dT%H:%M:%S"), "COUNTS": counts})


# ----------------------------------------------------------------- HEL1OS
def _band_range(extname: str) -> tuple[float, float] | None:
    m = _KEV_RANGE_RE.search(extname or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def _band_score(extname: str) -> float:
    """Rank HEL1OS CZT energy bands by FLARE SENSITIVITY (higher = better).

    We used to take the WIDEST band (the 18-160 keV total). Measured on real data, that is a
    poor choice: the 80-150 keV sub-band is particle-background dominated and DILUTES the signal.

        band            flare contrast (p99.9/median)   SNR
        20-40  keV              3.6                     5.2   <- best
        40-60  keV              3.0                     4.3
        80-150 keV              2.2                     2.7   <- background
        18-160 keV (total)      2.7                     3.7   <- what we used

    Solar flare hard X-ray emission is non-thermal bremsstrahlung concentrated at the LOW-energy
    end. So prefer the lowest-energy *sub*-band (not the total), which gives ~40% better SNR.
    """
    r = _band_range(extname)
    if r is None:
        return -1.0
    lo, hi = r
    width = hi - lo
    # the total/broad band spans nearly the whole range - deprioritise it
    if width >= 100.0:
        return 0.1
    # otherwise prefer the lowest-energy band
    return 1000.0 - hi


def read_hel1os_lc(path: Path) -> pd.DataFrame:
    """Read one HEL1OS lightcurve_*.fits -> DataFrame[ISOT, CTR] using the widest band."""
    from astropy.io import fits

    with fits.open(path, memmap=False) as hdul:
        candidates = [
            h for h in hdul
            if getattr(h, "data", None) is not None
            and getattr(h.data, "columns", None) is not None
            and "CTR" in h.columns.names
        ]
        if not candidates:
            raise ValueError(f"no CTR lightcurve HDU in {path.name}")
        best = max(candidates, key=lambda h: _band_score(h.header.get("EXTNAME", "")))
        data = best.data
        ctr = np.asarray(data["CTR"], dtype="float64")
        if "ISOT" in best.columns.names:
            isot = pd.Index([str(s).strip() for s in data["ISOT"]])
        else:  # fall back to MJD
            mjd = np.asarray(data["MJD"], dtype="float64")
            isot = pd.to_datetime(mjd + 2400000.5, unit="D", origin="julian", utc=True) \
                .strftime("%Y-%m-%dT%H:%M:%S.%f")
    return pd.DataFrame({"ISOT": isot, "CTR": ctr})


# ----------------------------------------------------------------- helpers
def _first_table_with(hdul, required: set):
    for hdu in hdul:
        cols = getattr(getattr(hdu, "data", None), "columns", None)
        if cols is not None and required.issubset(set(cols.names)):
            return hdu
    return None


def _extract_zip(zip_path: Path, dest: Path) -> Path:
    out = dest / zip_path.stem
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    return out


def _find(root: Path, *patterns: str) -> list[Path]:
    found: list[Path] = []
    for pat in patterns:
        found.extend(root.rglob(pat))
    return sorted(set(found))


# ----------------------------------------------------------------- per-archive
def convert_solexs_archive(zip_path: Path, tmp: Path) -> Path | None:
    """Extract a SoLEXS AL1_SLX_*.zip -> data/raw/solexs/<stem>.csv."""
    extracted = _extract_zip(zip_path, tmp)
    try:
        lc_files = _find(extracted, "*.lc.gz", "*.lc")
        if not lc_files:
            print(f"  [skip] {zip_path.name}: no .lc/.lc.gz inside")
            return None
        df = pd.concat([read_solexs_lc(p) for p in lc_files], ignore_index=True)
        # SDD1/SDD2 detectors may overlap in time -> average duplicates.
        df = df.groupby("ISOT", as_index=False)["COUNTS"].mean().sort_values("ISOT")
        config.SOLEXS_RAW_DIR.mkdir(parents=True, exist_ok=True)
        out = config.SOLEXS_RAW_DIR / f"{zip_path.stem}.csv"
        df.to_csv(out, index=False)
        print(f"  [ok]   {zip_path.name} -> solexs/{out.name} ({len(df):,} rows, {len(lc_files)} det)")
        return out
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


def convert_hel1os_archive(zip_path: Path, tmp: Path) -> Path | None:
    """Extract a HEL1OS HLS_*.zip -> data/raw/hel1os/<stem>.csv."""
    extracted = _extract_zip(zip_path, tmp)
    try:
        lc_file = None
        for name in HEL1OS_LIGHTCURVE_PREFERENCE:
            hits = _find(extracted, name)
            if hits:
                lc_file = hits[0]
                break
        if lc_file is None:
            hits = _find(extracted, "lightcurve_czt*.fits")
            lc_file = hits[0] if hits else None
        if lc_file is None:
            print(f"  [skip] {zip_path.name}: no lightcurve_czt*.fits inside")
            return None
        df = read_hel1os_lc(lc_file)
        config.HEL1OS_RAW_DIR.mkdir(parents=True, exist_ok=True)
        out = config.HEL1OS_RAW_DIR / f"{zip_path.stem}.csv"
        df.to_csv(out, index=False)
        print(f"  [ok]   {zip_path.name} -> hel1os/{out.name} ({len(df):,} rows, {lc_file.name})")
        return out
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


# ----------------------------------------------------------------- orchestration
def convert_all(source_dir: Path | None = None, include: list[str] | None = None) -> dict:
    """Convert every AL1_SLX_*.zip and HLS_*.zip in ``source_dir``.

    ``include`` (substrings) restricts which archives are converted, e.g.
    ``["202403"]`` for March 2024. Convert one contiguous window at a time so the
    1-Hz timeline does not span far-apart, mostly-empty date ranges.
    """
    source_dir = Path(source_dir) if source_dir else config.default_incoming_dir()

    def keep(p: Path) -> bool:
        return not include or any(s in p.name for s in include)

    sxr_zips = [p for p in sorted(source_dir.glob("AL1_SLX_*.zip")) if keep(p)]
    hxr_zips = [p for p in sorted(source_dir.glob("HLS_*.zip")) if keep(p)]
    print(f"Scanning {source_dir}")
    if include:
        print(f"  filter: filename contains one of {include}")
    print(f"  found {len(sxr_zips)} SoLEXS + {len(hxr_zips)} HEL1OS archive(s)\n")

    written = {"solexs": 0, "hel1os": 0}
    with tempfile.TemporaryDirectory(prefix="pradan_") as td:
        tmp = Path(td)
        if sxr_zips:
            print("SoLEXS (soft X-ray):")
            for z in sxr_zips:
                try:
                    if convert_solexs_archive(z, tmp):
                        written["solexs"] += 1
                except Exception as e:  # one bad archive must not abort the batch
                    print(f"  [err]  {z.name}: {e}")
        if hxr_zips:
            print("\nHEL1OS (hard X-ray):")
            for z in hxr_zips:
                try:
                    if convert_hel1os_archive(z, tmp):
                        written["hel1os"] += 1
                except Exception as e:
                    print(f"  [err]  {z.name}: {e}")

    print(f"\nDone. Wrote {written['solexs']} SoLEXS + {written['hel1os']} HEL1OS CSV(s) "
          f"into {config.RAW_DIR}.")
    if written["solexs"] == 0 or written["hel1os"] == 0:
        print("NOTE: the pipeline needs BOTH soft (SoLEXS) and hard (HEL1OS) X-ray data "
              "that OVERLAP IN TIME for the fusion step to combine them.")
    return written
