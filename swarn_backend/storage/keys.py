"""Object key layout (section 8) plus file-kind and content-type rules."""

from __future__ import annotations

import mimetypes
import posixpath

# workspace sub-paths the push sync never uploads
SKIP_DIRS = {".swarn_datasets", "__pycache__", ".registry", "uploads"}
SKIP_FILE_PREFIXES = ("_exec_", "_swarn_bootstrap")

_CONTENT_TYPES = {
    ".parquet": "application/vnd.apache.parquet",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".csv": "text/csv; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".yaml": "text/yaml; charset=utf-8",
    ".yml": "text/yaml; charset=utf-8",
    ".pkl": "application/octet-stream",
    ".joblib": "application/octet-stream",
    ".onnx": "application/octet-stream",
    ".pt": "application/octet-stream",
    ".db": "application/octet-stream",
}


def upload_key(user_id, batch_id, filename: str) -> str:
    return f"users/{user_id}/uploads/{batch_id}/{filename}"


def workspace_file_key(workspace_id, rel_path: str) -> str:
    return f"workspaces/{workspace_id}/files/{rel_path}"


def workspace_dataset_key(workspace_id, name: str) -> str:
    return f"workspaces/{workspace_id}/datasets/{name}.parquet"


def workspace_prefix(workspace_id, sub: str) -> str:
    """`.chroma`, `artifacts`, `eval_runs`."""
    return f"workspaces/{workspace_id}/{sub}/"


def session_key(session_id, filename: str = "trace.json") -> str:
    return f"sessions/{session_id}/{filename}"


def run_prefix(run_id: str) -> str:
    return f"runs/{run_id}/"


KNOWLEDGE_PREFIX = "knowledge/"


def content_type_for(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    if ext == ".json" and path.endswith(".vl.json"):
        return "application/json"
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guess, _ = mimetypes.guess_type(path)
    return guess or "application/octet-stream"


def file_kind(path: str) -> str:
    """chart | spec | report | table | model | other — agent2's artifacts.py rule."""
    lower = path.lower()
    name = posixpath.basename(lower)
    if lower.endswith(".vl.json"):
        return "spec"
    ext = posixpath.splitext(lower)[1]
    if ext in (".png", ".svg", ".jpg", ".jpeg"):
        return "chart"
    if name.endswith("_report.html") or name.endswith("_report.md"):
        return "report"
    if ext in (".csv", ".parquet"):
        return "table"
    if ext in (".pkl", ".joblib", ".onnx", ".pt"):
        return "model"
    return "other"


def is_inline(content_type: str) -> bool:
    return content_type.startswith(("image/", "text/html", "text/markdown", "text/plain",
                                    "application/json"))
