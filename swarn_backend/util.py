"""Small shared helpers."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


def epoch(dt: datetime | None) -> float | None:
    """A timestamptz as the epoch-seconds float the API contract uses."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def from_epoch(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(float(ts), tz=UTC)


def parse_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def safe_filename(name: str) -> str:
    """agent2's rule (paths.safe_filename): alnum, `_` and `-` only."""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)


def sanitize_upload_name(filename: str | None) -> str:
    """Stem and extension sanitised separately so `movies.csv` keeps its dot
    (mirrors agent2's dashboard.api_upload)."""
    import os
    stem, ext = os.path.splitext(os.path.basename(filename or "upload.dat"))
    name = safe_filename(stem) or "upload"
    if ext:
        name += "." + safe_filename(ext.lstrip("."))
    return name


_REL_PATH_OK = re.compile(r"^[^\0]*$")


def clean_relative_path(path: str) -> str | None:
    """Normalise a workspace-relative path; None when it escapes or is absolute."""
    if path is None:
        return ""
    p = str(path).replace("\\", "/").strip()
    if p in ("", ".", "./"):
        return ""
    if p.startswith("/") or not _REL_PATH_OK.match(p):
        return None
    parts: list[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts)
