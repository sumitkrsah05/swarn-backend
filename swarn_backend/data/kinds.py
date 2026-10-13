"""Column typing and cell serialisation — the exact rules of agent2's
agent/web/data_api.py, so the grid sees the same kinds it always did."""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any

import numpy as np
import pandas as pd


def column_kind(series: pd.Series) -> str:
    """number | string | date | datetime | boolean | other."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        sample = series.dropna().head(1000)
        if len(sample) and bool(sample.dt.normalize().eq(sample).all()):
            return "date"
        return "datetime"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    if isinstance(dtype, pd.CategoricalDtype):
        return "string"
    if pd.api.types.is_string_dtype(dtype) or dtype == object:
        return "string"
    return "other"


def column_descriptors(frame: pd.DataFrame) -> list[dict]:
    return [{"name": str(col), "dtype": str(frame[col].dtype), "kind": column_kind(frame[col])}
            for col in frame.columns]


def cell(value: Any) -> Any:
    """One cell as JSON: NaN/NaT/NA → null, timestamps → ISO strings, numpy
    scalars → plain numbers, anything else → str()."""
    if value is None or value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        return value
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, np.datetime64):
        ts = pd.Timestamp(value)
        return None if pd.isna(ts) else ts.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)
