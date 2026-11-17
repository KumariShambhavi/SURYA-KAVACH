"""
schema.py — the canonical light-curve data contract and a lightweight validator.

Every loader (real or synthetic) MUST return a DataFrame that passes
``validate_light_curve``. This decouples the data source from all downstream
algorithms: preprocess / nowcast / forecast never know whether the data came
from SoLEXS+HEL1OS FITS, CSVs, or a synthetic generator.

(We hand-roll validation instead of depending on pandera to keep the dependency
surface minimal and the failure messages domain-specific.)
"""
from __future__ import annotations

import pandas as pd
from pandas.api import types as pdt

from . import config


class SchemaError(ValueError):
    """Raised when a light-curve DataFrame violates the canonical contract."""


def validate_light_curve(df: pd.DataFrame, *, allow_empty: bool = False) -> pd.DataFrame:
    """Validate and return ``df`` (unchanged) or raise SchemaError.

    Checks: required columns present; time is UTC datetime, monotonic increasing,
    unique; flux columns numeric; quality columns are 0/1 integers.
    """
    missing = [c for c in config.REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise SchemaError(f"missing required columns: {missing}; got {list(df.columns)}")

    if not allow_empty and len(df) == 0:
        raise SchemaError("light curve is empty")

    t = df[config.TIME_COL]
    if not pdt.is_datetime64_any_dtype(t):
        raise SchemaError(f"'{config.TIME_COL}' must be datetime64, got {t.dtype}")
    if getattr(t.dtype, "tz", None) is None:
        raise SchemaError(f"'{config.TIME_COL}' must be timezone-aware (UTC)")
    if len(df) > 1:
        if not t.is_monotonic_increasing:
            raise SchemaError("time must be monotonic increasing")
        if t.duplicated().any():
            raise SchemaError("time has duplicate values")

    for col in (config.SXR_COL, config.HXR_COL):
        if not pdt.is_numeric_dtype(df[col]):
            raise SchemaError(f"'{col}' must be numeric, got {df[col].dtype}")

    for col in (config.SXR_QUALITY_COL, config.HXR_QUALITY_COL):
        if not pdt.is_integer_dtype(df[col]):
            raise SchemaError(f"'{col}' must be integer, got {df[col].dtype}")
        bad = set(pd.unique(df[col])) - {0, 1}
        if bad:
            raise SchemaError(f"'{col}' must be 0/1, found {sorted(bad)}")

    return df


def is_valid(df: pd.DataFrame) -> bool:
    try:
        validate_light_curve(df)
        return True
    except SchemaError:
        return False
