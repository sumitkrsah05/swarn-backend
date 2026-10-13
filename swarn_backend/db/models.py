"""SQLAlchemy 2.0 models — section 7 of docs/Backend_Build_Instructions.md.

Conventions: UUID keys unless agent2 already names the thing (session uuid,
12-hex job id, AIDE run id, 8-hex node id); timestamptz; jsonb payloads;
user_id on every owned table; ledgers are append-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
                        Numeric, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # every datetime column is timestamptz (section 7 conventions)
    type_annotation_map = {datetime: DateTime(timezone=True)}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now_col(**kw):
    return mapped_column(server_default=func.now(), **kw)


# ─── identity ─────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _now_col()
    last_login_at: Mapped[datetime | None] = mapped_column()


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               index=True, nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="browser", nullable=False)  # browser | cli
    label: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now_col()
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = _now_col()
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_to: Mapped[str | None] = mapped_column(Text)
    cli_code: Mapped[str | None] = mapped_column(String(64))   # set when the login authorises a CLI
    created_at: Mapped[datetime] = _now_col()


class CliLoginCode(Base):
    """The browser-mediated CLI sign-in (section 9.6 discussion)."""
    __tablename__ = "cli_login_codes"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)  # pending|approved|claimed|expired
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_plain: Mapped[str | None] = mapped_column(Text)     # held until the CLI claims it, then nulled
    created_at: Mapped[datetime] = _now_col()
    expires_at: Mapped[datetime] = mapped_column(nullable=False)


# ─── workspaces and inputs ────────────────────────────────────────────────────


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column()
    client_state: Mapped[dict | None] = mapped_column(JSONB)


class UploadBatch(Base):
    __tablename__ = "upload_batches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               index=True, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()
    deleted_at: Mapped[datetime | None] = mapped_column()


class UploadFile(Base):
    __tablename__ = "upload_files"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("upload_batches.id", ondelete="CASCADE"),
                                                index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    content_type: Mapped[str | None] = mapped_column(String(200))
    object_key: Mapped[str] = mapped_column(Text, nullable=False)


# ─── work ─────────────────────────────────────────────────────────────────────


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"),
                                                    nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    data_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("upload_batches.id"))
    data_dir: Mapped[str | None] = mapped_column(Text)          # as the client sent it
    steps: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    model: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    resume_session_ids: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list,
                                                          nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now_col()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    n_events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_event: Mapped[dict | None] = mapped_column(JSONB)
    eval_config: Mapped[dict | None] = mapped_column(JSONB)
    eval_options: Mapped[dict | None] = mapped_column(JSONB)
    worker_pid: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_jobs_user_created", "user_id", "created_at"),
        Index("ix_jobs_workspace_created", "workspace_id", "created_at"),
        Index("ix_jobs_status", "status"),
    )


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[float] = mapped_column(Float, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_job_events_seq"),)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True,
                                        nullable=False)
    checkpoint: Mapped[str | None] = mapped_column(String(200))
    question: Mapped[str | None] = mapped_column(Text)
    options: Mapped[list | None] = mapped_column(JSONB)
    default_answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    via: Mapped[str | None] = mapped_column(String(32))
    requested_at: Mapped[float | None] = mapped_column(Float)
    resolved_at: Mapped[datetime | None] = mapped_column()


# ─── records ──────────────────────────────────────────────────────────────────


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               index=True, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"),
                                                    index=True, nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    task: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[float | None] = mapped_column(Float)
    ended_at: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    corrections: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_s: Mapped[float | None] = mapped_column(Float)
    tool_counts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    trace_key: Mapped[str | None] = mapped_column(Text)
    n_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _now_col()


class SessionStep(Base):
    __tablename__ = "session_steps"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ts: Mapped[float] = mapped_column(Float, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_session_steps_seq"),)


class SearchRun(Base):
    __tablename__ = "search_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               index=True, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"),
                                                    index=True, nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    task: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    steps_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    nodes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    best_metric: Mapped[float | None] = mapped_column(Float)
    lower_is_better: Mapped[bool | None] = mapped_column(Boolean)
    holdout_metric: Mapped[float | None] = mapped_column(Float)
    holdout_note: Mapped[str | None] = mapped_column(Text)
    stopped_because: Mapped[str | None] = mapped_column(Text)
    exported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    run_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    report_key: Mapped[str | None] = mapped_column(Text)
    solution_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()
    finished_at: Mapped[datetime | None] = mapped_column()


class SearchNode(Base):
    __tablename__ = "search_nodes"
    run_id: Mapped[str] = mapped_column(ForeignKey("search_runs.id", ondelete="CASCADE"),
                                        primary_key=True)
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(16))
    step: Mapped[int | None] = mapped_column(Integer)
    stage: Mapped[str | None] = mapped_column(String(16))
    is_buggy: Mapped[bool | None] = mapped_column(Boolean)
    suspicious: Mapped[bool | None] = mapped_column(Boolean)
    metric: Mapped[float | None] = mapped_column(Float)
    exec_time: Mapped[float | None] = mapped_column(Float)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    timed_out: Mapped[bool | None] = mapped_column(Boolean)
    ctime: Mapped[float | None] = mapped_column(Float)
    analysis: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"),
                                                    index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    rows: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cols: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    columns: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    parents: Mapped[list[str]] = mapped_column(ARRAY(String(300)), default=list, nullable=False)
    tool: Mapped[str | None] = mapped_column(String(100))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[str | None] = mapped_column(String(12))
    step: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[float | None] = mapped_column(Float)      # epoch, from the lineage
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        Index("ix_datasets_active_name", "workspace_id", "name", unique=True,
              postgresql_where="superseded_at IS NULL"),
    )


class File(Base):
    __tablename__ = "files"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"),
                                                    index=True, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="other", nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(200))
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    job_id: Mapped[str | None] = mapped_column(String(12))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    step: Mapped[int | None] = mapped_column(Integer)
    dataset: Mapped[str | None] = mapped_column(String(300))
    spec_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        Index("ix_files_active_path", "workspace_id", "path", unique=True,
              postgresql_where="deleted_at IS NULL"),
    )


# ─── ledgers ──────────────────────────────────────────────────────────────────


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                               index=True, nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(12))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str | None] = mapped_column(String(200))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    currency: Mapped[str | None] = mapped_column(String(8))
    occurred_at: Mapped[datetime] = _now_col()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    occurred_at: Mapped[datetime] = _now_col()
