"""The line protocol between the API (supervisor) and a worker subprocess.

stdin  (API → worker): first line is the `WorkerSpec` JSON; every later line
                       is a command `{"c": "approve"|"cancel", ...}`.
stdout (worker → API): one JSON object per line, `{"t": <type>, ...}`.

Event types (section 10.2 of the build document):
  status, session, session_step, session_closed, usage, node,
  approval_request, approval_resolved, approval_nack, progress,
  artifact_files, dataset_dumped, log, done
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

EVENT_TYPES = {"status", "session", "session_step", "session_closed", "usage", "node",
               "approval_request", "approval_resolved", "approval_nack", "progress",
               "artifact_files", "dataset_dumped", "log", "done"}

# events that become job_events rows and `channel: "job"` frames, with the
# name the old API used
JOB_EVENT_NAMES = {
    "status": "status",
    "session": "session",
    "usage": "usage",
    "node": "node",
    "approval_request": "approval_request",
    "approval_resolved": "approval_answered",
    "progress": "progress",
}


class WorkerSpec(BaseModel):
    job_id: str
    method: str                                  # react | aide | team | eval
    task: str
    model: str = ""
    steps: int = 10
    data_dir: str | None = None                  # workspace-relative, e.g. uploads/<batch>
    resume_session_ids: list[str] = Field(default_factory=list)
    workspace_id: str
    user_id: str
    data_root: str                               # the staging directory (SWARN_DATA_ROOT)
    env: dict[str, str] = Field(default_factory=dict)
    eval_config: dict[str, Any] | None = None
    eval_options: dict[str, Any] = Field(default_factory=dict)
    registry_max_mb: int = 2048


def approve_command(request_id: str, answer: str) -> dict:
    return {"c": "approve", "request_id": request_id, "answer": answer}


def cancel_command() -> dict:
    return {"c": "cancel"}
