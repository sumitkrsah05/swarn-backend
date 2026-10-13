"""Configuration: every BACKEND_* variable, read once.

`.env` in the working directory is loaded into the process environment first
so the SWARN_* pass-through variables (section 13 of the build document) are
visible to `swarn_env()` as well as the BACKEND_* ones to pydantic.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(override=False)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BACKEND_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    # ── stores ──────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://swarn:swarn@127.0.0.1:5432/swarn_backend"
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "swarn"
    s3_region: str = "us-east-1"

    # ── identity ────────────────────────────────────────────────────────
    public_url: str = "http://localhost:8000"
    frontend_url: str = ""                      # defaults to public_url
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_email_domains: str = ""
    allowed_emails: str = ""
    admin_emails: str = ""
    secret_key: str = "change-me"
    cookie_name: str = "swarn_session"
    session_max_days: int = 7
    session_idle_hours: int = 12
    cli_session_max_days: int = 90
    insecure_cookies: bool = False
    dev_login: bool = False
    cors_origins: str = ""
    expose_docs: bool = False

    # ── jobs ────────────────────────────────────────────────────────────
    work_dir: str = "./work"
    worker_python: str = ""                     # defaults to sys.executable
    job_concurrency: int = 2
    max_running_per_user: int = 2
    approval_timeout_s: int = 1800
    cancel_grace_s: int = 60
    max_upload_mb: int = 512
    stage_max_gb: float = 20
    hydrate_max_mb: int = 2048
    dataset_cache_mb: int = 1024
    keep_failed_workdirs: bool = False
    playbook_cache_s: int = 60

    log_level: str = "INFO"

    # ── derived ─────────────────────────────────────────────────────────
    @property
    def frontend_base(self) -> str:
        return (self.frontend_url or self.public_url).rstrip("/")

    @property
    def public_base(self) -> str:
        return self.public_url.rstrip("/")

    @property
    def allowed_domains(self) -> list[str]:
        return [d.lower().lstrip("@") for d in _csv(self.allowed_email_domains)]

    @property
    def allowed_email_list(self) -> list[str]:
        return [e.lower() for e in _csv(self.allowed_emails)]

    @property
    def admin_email_list(self) -> list[str]:
        return [e.lower() for e in _csv(self.admin_emails)]

    @property
    def cors_origin_list(self) -> list[str]:
        return _csv(self.cors_origins)

    @property
    def work_path(self) -> Path:
        return Path(self.work_dir).resolve()

    @property
    def worker_executable(self) -> str:
        return self.worker_python or sys.executable

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def cookie_secure(self) -> bool:
        return not self.insecure_cookies

    def swarn_env(self) -> dict[str, str]:
        """Every SWARN_* variable in the environment, handed to workers verbatim
        (the backend sets the path variables itself, so those are dropped)."""
        skip = {"SWARN_DATA_ROOT", "SWARN_ARTIFACTS_DIR", "SWARN_EVAL_DIR",
                "SWARN_LEDGER_DIR", "SWARN_KNOWLEDGE_DIR", "SWARN_AUTO_APPROVE"}
        env = {k: v for k, v in os.environ.items() if k.startswith("SWARN_") and k not in skip}
        env.setdefault("SWARN_APPROVAL_TIMEOUT", str(self.approval_timeout_s))
        return env


@lru_cache
def get_settings() -> Settings:
    return Settings()
