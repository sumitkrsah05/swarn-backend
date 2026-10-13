"""The /api/data semantics on parquet-backed frames (ported from agent2's
test_web_data_api expectations)."""

import io
import json

import pandas as pd
import pytest
from fastapi import HTTPException

from swarn_backend.data import frames
from swarn_backend.data.kinds import column_descriptors, column_kind


@pytest.fixture
def frame():
    df = pd.DataFrame({
        "region": ["north", "south", "east", None, "west"],
        "revenue": [10.0, 20.5, None, 7.0, 1.0],
        "day": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]),
        "stamp": pd.to_datetime(["2026-01-01 10:00", "2026-01-02 11:30", "2026-01-03 00:00",
                                 "2026-01-04 09:15", "2026-01-05 23:59"]),
        "flag": [True, False, True, False, True],
    })
    return df


def test_kinds(frame):
    kinds = {c["name"]: c["kind"] for c in column_descriptors(frame)}
    assert kinds == {"region": "string", "revenue": "number", "day": "date",
                     "stamp": "datetime", "flag": "boolean"}


def test_rows_sort_nulls_last(frame):
    out = frames.rows_view(frame, "t", offset=0, limit=10, sort="revenue", desc=True, q="",
                           filters="")
    revenues = [r[1] for r in out["rows"]]
    assert revenues == [20.5, 10.0, 7.0, 1.0, None]
    assert out["total"] == 5 and out["limit"] == 10


def test_limit_cap_and_offset(frame):
    out = frames.rows_view(frame, "t", offset=3, limit=5000, sort="", desc=False, q="", filters="")
    assert out["limit"] == frames.MAX_LIMIT and len(out["rows"]) == 2 and out["offset"] == 3


def test_search_and_filters(frame):
    out = frames.rows_view(frame, "t", offset=0, limit=10, sort="", desc=False, q="ORTH", filters="")
    assert out["total"] == 1 and out["rows"][0][0] == "north"
    f = json.dumps([{"column": "revenue", "op": "range", "min": 5, "max": 15}])
    out = frames.rows_view(frame, "t", offset=0, limit=10, sort="", desc=False, q="", filters=f)
    assert sorted(r[1] for r in out["rows"]) == [7.0, 10.0]
    f = json.dumps([{"column": "region", "op": "in", "values": [None, "west"]}])
    out = frames.rows_view(frame, "t", offset=0, limit=10, sort="", desc=False, q="", filters=f)
    assert out["total"] == 2
    f = json.dumps([{"column": "day", "op": "range", "min": "2026-01-04"}])
    out = frames.rows_view(frame, "t", offset=0, limit=10, sort="", desc=False, q="", filters=f)
    assert out["total"] == 2


def test_bad_inputs_are_422(frame):
    with pytest.raises(HTTPException) as e:
        frames.rows_view(frame, "t", offset=0, limit=10, sort="nope", desc=False, q="", filters="")
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        frames.rows_view(frame, "t", offset=0, limit=10, sort="", desc=False, q="",
                         filters=json.dumps([{"column": "revenue", "op": "between"}]))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        frames.rows_view(frame, "t", offset=0, limit=10, sort="", desc=False, q="", filters="{not json")
    assert e.value.status_code == 422


def test_column_view(frame):
    out = frames.column_view(frame, "t", "revenue")
    assert out["kind"] == "number" and out["blanks"] == 1 and out["distinct"] == 4
    assert out["min"] == 1.0 and out["max"] == 20.5
    assert any(t["value"] == 20.5 for t in out["top"])
    with pytest.raises(HTTPException) as e:
        frames.column_view(frame, "t", "missing")
    assert e.value.status_code == 404


def test_cells_serialise(frame):
    out = frames.rows_view(frame, "t", offset=0, limit=1, sort="", desc=False, q="", filters="")
    row = out["rows"][0]
    assert row[2] == "2026-01-01T00:00:00" and row[4] is True


def test_parquet_roundtrip_keeps_kinds(frame):
    buf = io.BytesIO()
    frame.to_parquet(buf)
    back = frames.read_parquet_bytes(buf.getvalue())
    assert column_kind(back["day"]) == "date" and column_kind(back["stamp"]) == "datetime"


def test_csv_export_streams(frame):
    chunks = list(frames.iter_csv(frame))
    assert chunks[0].startswith("region,revenue,day,stamp,flag")
    assert "".join(chunks).count("\n") == 6


def test_cache_evicts_by_bytes(frame):
    cache = frames.FrameCache(max_bytes=1)
    cache.put("a", frame)
    cache.put("b", frame)
    assert cache.get("a") is None and cache.get("b") is not None
