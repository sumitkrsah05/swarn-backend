"""FastAPI dependencies: the signed-in user (cookie or bearer), admin gate,
request IP, and the Origin check for state-changing requests.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import AuthSession, User
from swarn_backend.settings import Settings, get_settings
from swarn_backend.util import now_utc

NOT_SIGNED_IN = HTTPException(401, "not signed in")


def client_ip(request: Request | WebSocket) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _bearer(request: Request | WebSocket) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def resolve_session(db: AsyncSession, settings: Settings, token: str | None
                          ) -> tuple[AuthSession, User] | None:
    if not token:
        return None
    found = await repo.load_auth_session(db, token)
    if found is None:
        return None
    session, user = found
    now = now_utc()
    if session.expires_at < now or not user.active:
        await repo.delete_auth_session(db, session.id)
        return None
    if session.kind == "browser":
        idle = timedelta(hours=settings.session_idle_hours)
        if session.last_seen_at + idle < now:
            await repo.delete_auth_session(db, session.id)
            return None
    # one write per minute at most; the UI polls every few seconds
    if (now - session.last_seen_at).total_seconds() > 60:
        await repo.touch_auth_session(db, session)
    return session, user


async def current_session(request: Request, db: AsyncSession = Depends(get_db),
                          settings: Settings = Depends(get_settings)) -> tuple[AuthSession, User]:
    token = _bearer(request) or request.cookies.get(settings.cookie_name)
    found = await resolve_session(db, settings, token)
    if found is None:
        raise NOT_SIGNED_IN
    return found


async def current_user(found: tuple[AuthSession, User] = Depends(current_session)) -> User:
    return found[1]


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(404, "not found")
    return user


def origin_allowed(request: Request, settings: Settings) -> bool:
    """State-changing requests must come from our own origin (or a listed
    CORS origin). Browsers always send Origin on POST; curl with a bearer
    token sends none, which is allowed."""
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer is None:
            return True
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = {settings.public_base, settings.frontend_base, *settings.cors_origin_list}
    return origin.rstrip("/") in allowed
