"""Paged, sorted, searched and filtered views of a parquet dataset — the
semantics of agent2's `/api/data/*` (agent/web/data_api.py) over frames read
from MinIO instead of a process-global registry.

`FrameCache` keeps recently used frames in memory (LRU by bytes).
"""

from __future__ import annotations

import io
import json
import re
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

import pandas as pd
from fastapi import HTTPException

from swarn_backend.data.kinds import cell, column_descriptors, column_kind

DEFAULT_LIMIT = 500
MAX_LIMIT = 1000
TOP_VALUES = 100
EXPORT_CHUNK_ROWS = 20_000
FILTER_OPS = ("range", "in", "contains")

NOT_IN_MEMORY = ("Dataset '{name}' is not in memory — the server may have restarted. "
                 "Re-run the step that created it.")


class FrameCache:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, tuple[pd.DataFrame, int]] = OrderedDict()
        self._bytes = 0

    def get(self, key: str) -> pd.DataFrame | None:
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item[0]

    def put(self, key: str, frame: pd.DataFrame) -> None:
        size = int(frame.memory_usage(deep=True).sum())
        if key in self._items:
            self._bytes -= self._items.pop(key)[1]
        self._items[key] = (frame, size)
        self._bytes += size
        while self._bytes > self.max_bytes and len(self._items) > 1:
            _, (_, s) = self._items.popitem(last=False)
            self._bytes -= s

    def drop(self, key: str) -> None:
        if key in self._items:
            self._bytes -= self._items.pop(key)[1]


def read_parquet_bytes(data: bytes) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(data))


# ─── filtering and sorting ────────────────────────────────────────────────────


def _as_text(series: pd.Series) -> pd.Series:
    if pd.api.types.is_string_dtype(series.dtype) and not isinstance(series.dtype, pd.CategoricalDtype):
        return series
    return series.astype("string")


def parse_filters(raw: str | None, frame: pd.DataFrame) -> list[dict]:
    if not raw:
        return []
    try:
        filters = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, f"filters must be URL-encoded JSON: {e}")
    if not isinstance(filters, list):
        raise HTTPException(422, "filters must be a JSON list of {column, op, ...} objects")
    columns = {str(c) for c in frame.columns}
    for i, f in enumerate(filters):
        if not isinstance(f, dict):
            raise HTTPException(422, f"filters[{i}] must be an object")
        column = f.get("column")
        if not isinstance(column, str) or column not in columns:
            raise HTTPException(422, f"filters[{i}]: unknown column {column!r}")
        if f.get("op") not in FILTER_OPS:
            raise HTTPException(422, f"filters[{i}]: op must be one of {list(FILTER_OPS)}")
    return filters


def _coerce_bound(series: pd.Series, value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None
    try:
        if kind in ("date", "datetime"):
            ts = pd.Timestamp(value)
            tz = getattr(series.dt, "tz", None)
            if tz is not None and ts.tzinfo is None:
                ts = ts.tz_localize(tz)
            return ts
        if kind == "boolean":
            return bool(value) if not isinstance(value, str) else value.strip().lower() in ("1", "true", "yes")
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"range bound {value!r} does not fit column '{series.name}'")


def _apply_filter(frame: pd.DataFrame, f: dict) -> pd.Series:
    series = frame[f["column"]]
    kind = column_kind(series)
    op = f["op"]
    if op == "range":
        lo = _coerce_bound(series, f.get("min"), kind)
        hi = _coerce_bound(series, f.get("max"), kind)
        if kind in ("string", "other"):
            text = _as_text(series)
            mask = text.notna()
            if lo is not None:
                mask &= text >= str(f.get("min"))
            if hi is not None:
                mask &= text <= str(f.get("max"))
            return mask
        mask = series.notna()
        if lo is not None:
            mask &= series >= lo
        if hi is not None:
            mask &= series <= hi
        return mask
    if op == "in":
        values = f.get("values")
        if not isinstance(values, list):
            raise HTTPException(422, f"filters on '{f['column']}': op 'in' needs a `values` list")
        want_null = any(v is None for v in values)
        concrete = [v for v in values if v is not None]
        mask = series.isna() if want_null else pd.Series(False, index=series.index)
        if concrete:
            if kind == "number":
                nums = []
                for v in concrete:
                    try:
                        nums.append(float(v))
                    except (TypeError, ValueError):
                        pass
                mask |= series.isin(nums)
            elif kind in ("date", "datetime"):
                stamps = []
                for v in concrete:
                    try:
                        stamps.append(pd.Timestamp(v))
                    except (TypeError, ValueError):
                        pass
                mask |= series.isin(stamps)
            elif kind == "boolean":
                mask |= series.isin([_coerce_bound(series, v, "boolean") for v in concrete])
            else:
                mask |= _as_text(series).isin([str(v) for v in concrete]).fillna(False)
        return mask
    text = str(f.get("text") or "")
    if not text:
        return pd.Series(True, index=series.index)
    return _as_text(series).str.contains(text, case=False, regex=False, na=False)


def _search(frame: pd.DataFrame, q: str) -> pd.Series:
    mask = pd.Series(False, index=frame.index)
    for col in frame.columns:
        series = frame[col]
        if column_kind(series) != "string":
            continue
        mask |= _as_text(series).str.contains(q, case=False, regex=False, na=False)
    return mask


def select_rows(frame: pd.DataFrame, q: str, filters: list[dict], sort: str, desc: bool) -> pd.DataFrame:
    if filters or q:
        mask = pd.Series(True, index=frame.index)
        for f in filters:
            mask &= _apply_filter(frame, f)
        if q:
            mask &= _search(frame, q)
        frame = frame[mask]
    if sort:
        frame = frame.sort_values(sort, ascending=not desc, kind="mergesort", na_position="last")
    return frame


# ─── views ────────────────────────────────────────────────────────────────────


def rows_view(frame: pd.DataFrame, name: str, *, offset: int, limit: int, sort: str, desc: bool,
              q: str, filters: str) -> dict:
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_LIMIT))
    columns = {str(c) for c in frame.columns}
    if sort and sort not in columns:
        raise HTTPException(422, f"unknown sort column {sort!r}")
    parsed = parse_filters(filters, frame)
    selected = select_rows(frame, q, parsed, sort, desc)
    page = selected.iloc[offset:offset + limit]
    return {
        "name": name,
        "total": int(len(selected)),
        "offset": offset,
        "limit": limit,
        "columns": column_descriptors(frame),
        "rows": [[cell(v) for v in row] for row in page.itertuples(index=False, name=None)],
    }


def column_view(frame: pd.DataFrame, name: str, column: str) -> dict:
    if column not in {str(c) for c in frame.columns}:
        raise HTTPException(404, f"Dataset '{name}' has no column '{column}'")
    series = frame[column]
    kind = column_kind(series)
    non_null = series.dropna()
    out: dict[str, Any] = {
        "rows": int(len(series)),
        "distinct": int(non_null.nunique()) if len(non_null) else 0,
        "blanks": int(series.isna().sum()),
        "kind": kind,
    }
    if len(non_null):
        if kind == "number":
            out["min"] = cell(non_null.min())
            out["max"] = cell(non_null.max())
            out["mean"] = cell(float(non_null.mean()))
        elif kind in ("date", "datetime"):
            out["min"] = cell(non_null.min())
            out["max"] = cell(non_null.max())
    counts = series.value_counts(dropna=True).head(TOP_VALUES)
    out["top"] = [{"value": cell(v), "count": int(n)} for v, n in counts.items()]
    return out


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip(".") or "dataset"


def iter_csv(frame: pd.DataFrame) -> Iterator[str]:
    yield frame.iloc[0:0].to_csv(index=False)
    for start in range(0, len(frame), EXPORT_CHUNK_ROWS):
        yield frame.iloc[start:start + EXPORT_CHUNK_ROWS].to_csv(index=False, header=False)


def dataset_entry(row: Any, frames_by_name: dict[str, list[dict]]) -> dict:
    """The `GET /api/data/datasets` entry from a `datasets` row. `derived_columns`
    are the columns absent from every parent that is still active."""
    parents = [p for p in (row.parents or []) if isinstance(p, str)]
    parent_columns: set[str] = set()
    for p in parents:
        for c in frames_by_name.get(p, []) or []:
            parent_columns.add(c["name"])
    columns = row.columns or []
    return {
        "name": row.name,
        "rows": int(row.rows),
        "cols": int(row.cols),
        "columns": columns,
        "parents": parents,
        "tool": row.tool,
        "session_id": str(row.session_id) if row.session_id else None,
        "step": row.step,
        "created_at": row.created_at,
        "derived_columns": [c["name"] for c in columns if c["name"] not in parent_columns]
                           if parent_columns else [],
    }
