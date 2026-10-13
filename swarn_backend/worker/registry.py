"""Hydrate agent2's in-memory dataset registry from .registry/ at job start
and dump changed frames back as parquet (section 11)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from swarn_backend.data.kinds import column_descriptors


def _registry_filename(name: str) -> str:
    import hashlib
    safe = "".join(c if c.isalnum() or c in "_-." else "_" for c in name)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:80]}-{digest}.parquet"


class RegistrySync:
    def __init__(self, pipeline: Any, registry_dir: Path, max_mb: int, log) -> None:
        self.pipe = pipeline
        self.dir = registry_dir
        self.max_bytes = max_mb * (1 << 20)
        self._log = log
        self._seen: dict[str, int] = {}          # name -> id(frame) at last dump

    # ── hydrate ─────────────────────────────────────────────────────────

    def hydrate(self) -> int:
        manifest = self.dir / "manifest.json"
        if not manifest.is_file():
            return 0
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            return 0
        # largest last so the cap drops the biggest frames first
        entries.sort(key=lambda e: int(e.get("bytes") or 0))
        loaded = 0
        total = 0
        for e in entries:
            path = self.dir / e["file"]
            if not path.is_file():
                continue
            size = os.path.getsize(path)
            if total + size > self.max_bytes:
                self._log("warning", f"registry: skipped '{e['name']}' ({size >> 20} MB) — over cap")
                continue
            try:
                frame = pd.read_parquet(path)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"registry: could not read '{e['name']}': {exc}")
                continue
            total += size
            self.pipe.datasets[e["name"]] = frame
            lineage = getattr(self.pipe, "lineage", None)
            if isinstance(lineage, dict):
                lineage[e["name"]] = {"parents": e.get("parents") or [], "tool": e.get("tool"),
                                      "session_id": e.get("session_id"), "step": e.get("step"),
                                      "created_at": e.get("created_at")}
            self._seen[e["name"]] = id(frame)
            loaded += 1
        return loaded

    # ── dump ────────────────────────────────────────────────────────────

    def dump(self, name: str, force: bool = False) -> dict | None:
        frame = self.pipe.datasets.get(name)
        if not isinstance(frame, pd.DataFrame):
            return None
        if not force and self._seen.get(name) == id(frame):
            return None
        out = frame
        if any(not isinstance(c, str) for c in frame.columns):
            out = frame.copy()
            out.columns = [str(c) for c in out.columns]
        fname = _registry_filename(name)
        path = self.dir / fname
        try:
            out.to_parquet(path)
        except Exception as exc:  # noqa: BLE001 — a frame parquet cannot hold is not fatal
            self._log("warning", f"registry: could not dump '{name}': {exc}")
            return None
        self._seen[name] = id(frame)
        lineage = (getattr(self.pipe, "lineage", {}) or {}).get(name) or {}
        return {
            "name": name,
            "path": f".registry/{fname}",
            "rows": int(frame.shape[0]),
            "cols": int(frame.shape[1]),
            "columns": column_descriptors(frame),
            "parents": [p for p in lineage.get("parents", []) if isinstance(p, str)],
            "tool": lineage.get("tool"),
            "session_id": lineage.get("session_id"),
            "step": lineage.get("step"),
            "created_at": lineage.get("created_at"),
            "bytes": os.path.getsize(path),
        }

    def dump_changed(self) -> list[dict]:
        out = []
        for name in list(self.pipe.datasets.keys()):
            info = self.dump(name)
            if info:
                out.append(info)
        return out
