"""Sign-in: Google OIDC for the browser, the browser-mediated flow for the
CLI, session management. Section 9 of the build document."""

from __future__ import annotations

from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.auth.google import GoogleOIDC, OIDCError, email_allowed
from swarn_backend.auth.tokens import new_state, pkce_pair, sign_ws_token
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import AuthSession, User
from swarn_backend.settings import Settings, get_settings
from swarn_backend.util import now_utc

router = APIRouter(prefix="/api/auth", tags=["auth"])

_oidc: GoogleOIDC | None = None


def oidc(settings: Settings) -> GoogleOIDC:
    global _oidc
    if _oidc is None:
        _oidc = GoogleOIDC(settings.google_client_id, settings.google_client_secret)
    return _oidc


def _callback_uri(settings: Settings) -> str:
    return f"{settings.public_base}/api/auth/google/callback"


def _safe_redirect(settings: Settings, target: str | None) -> str:
    """Only relative paths, so the callback can never be an open redirect."""
    if not target or not target.startswith("/") or target.startswith("//"):
        target = "/"
    return settings.frontend_base + target


def set_cookie(resp: Response, settings: Settings, token: str, max_age: int) -> None:
    resp.set_cookie(settings.cookie_name, token, max_age=max_age, path="/", httponly=True,
                    secure=settings.cookie_secure, samesite="lax")


def clear_cookie(resp: Response, settings: Settings) -> None:
    resp.delete_cookie(settings.cookie_name, path="/")


def user_json(user: User) -> dict:
    return {"id": str(user.id), "email": user.email, "display_name": user.display_name,
            "avatar_url": user.avatar_url, "role": user.role}


async def _sign_in(db: AsyncSession, settings: Settings, request: Request, *, sub: str,
                   email: str, name: str | None, picture: str | None) -> tuple[User, str]:
    user = await repo.upsert_google_user(db, sub=sub, email=email, name=name, picture=picture,
                                         make_admin=email.lower() in settings.admin_email_list)
    if not user.active:
        raise HTTPException(403, "account deactivated")
    _, token = await repo.create_auth_session(db, user, kind="browser",
                                              max_days=settings.session_max_days,
                                              user_agent=request.headers.get("user-agent"),
                                              ip=deps.client_ip(request))
    await repo.audit(db, user, "login", "user", str(user.id), ip=deps.client_ip(request))
    await db.commit()
    return user, token


# ─── Google ───────────────────────────────────────────────────────────────────


@router.get("/google/login")
async def google_login(request: Request, redirect_to: str = "/", cli_code: str | None = None,
                       db: AsyncSession = Depends(get_db),
                       settings: Settings = Depends(get_settings)):
    if not settings.google_enabled:
        raise HTTPException(503, "Google sign-in is not configured (BACKEND_GOOGLE_CLIENT_ID)")
    state = new_state()
    verifier, challenge = pkce_pair()
    await repo.save_oauth_state(db, state, verifier, redirect_to, cli_code=cli_code)
    domains = settings.allowed_domains
    url = await oidc(settings).auth_url(redirect_uri=_callback_uri(settings), state=state,
                                        code_challenge=challenge,
                                        hd=domains[0] if len(domains) == 1 else None)
    return RedirectResponse(url, status_code=302)


@router.get("/google/callback")
async def google_callback(request: Request, code: str | None = None, state: str | None = None,
                          error: str | None = None, db: AsyncSession = Depends(get_db),
                          settings: Settings = Depends(get_settings)):
    fail = settings.frontend_base + "/login?error="
    if error or not code or not state:
        return RedirectResponse(fail + quote(error or "missing_code"), status_code=302)
    saved = await repo.pop_oauth_state(db, state)
    if saved is None:
        return RedirectResponse(fail + "bad_state", status_code=302)
    try:
        tokens = await oidc(settings).exchange(code=code, code_verifier=saved.code_verifier,
                                               redirect_uri=_callback_uri(settings))
        claims = await oidc(settings).verify_id_token(tokens.get("id_token", ""))
    except OIDCError as e:
        await repo.audit(db, None, "login.failed", details={"reason": str(e)[:200]},
                         ip=deps.client_ip(request))
        return RedirectResponse(fail + "oidc", status_code=302)
    email = str(claims.get("email") or "").lower()
    if not email or not claims.get("email_verified", False):
        return RedirectResponse(fail + "unverified_email", status_code=302)
    if not email_allowed(email, claims.get("hd"), domains=settings.allowed_domains,
                         emails=settings.allowed_email_list):
        await repo.audit(db, None, "login.refused", details={"email": email},
                         ip=deps.client_ip(request))
        return RedirectResponse(fail + "not_allowed", status_code=302)
    user, token = await _sign_in(db, settings, request, sub=str(claims["sub"]), email=email,
                                 name=claims.get("name"), picture=claims.get("picture"))
    if saved.cli_code:
        target = f"{settings.public_base}/api/auth/cli?code={quote(saved.cli_code)}"
    else:
        target = _safe_redirect(settings, saved.redirect_to)
    resp = RedirectResponse(target, status_code=302)
    set_cookie(resp, settings, token, settings.session_max_days * 86400)
    return resp


@router.get("/dev-login")
async def dev_login(request: Request, email: str, redirect_to: str = "/",
                    db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Local development without Google: BACKEND_DEV_LOGIN=1 and no client id."""
    if not settings.dev_login or settings.google_enabled:
        raise HTTPException(404, "not found")
    host = urlparse(settings.public_base).hostname or ""
    if host not in ("localhost", "127.0.0.1"):
        raise HTTPException(404, "not found")
    user, token = await _sign_in(db, settings, request, sub=f"dev:{email.lower()}", email=email,
                                 name=email.split("@")[0], picture=None)
    resp = RedirectResponse(_safe_redirect(settings, redirect_to), status_code=302)
    set_cookie(resp, settings, token, settings.session_max_days * 86400)
    return resp


# ─── session ──────────────────────────────────────────────────────────────────


@router.get("/me")
async def me(user: User = Depends(deps.current_user)):
    return user_json(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response,
                 found: tuple[AuthSession, User] = Depends(deps.current_session),
                 db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    session, user = found
    await repo.delete_auth_session(db, session.id)
    await repo.audit(db, user, "logout", ip=deps.client_ip(request))
    await db.commit()
    clear_cookie(response, settings)
    return Response(status_code=204)


@router.post("/logout-all", status_code=204)
async def logout_all(request: Request, response: Response, user: User = Depends(deps.current_user),
                     db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    await repo.delete_user_sessions(db, user.id)
    await repo.audit(db, user, "logout.all", ip=deps.client_ip(request))
    await db.commit()
    clear_cookie(response, settings)
    return Response(status_code=204)


@router.post("/ws-token")
async def ws_token(user: User = Depends(deps.current_user),
                   settings: Settings = Depends(get_settings)):
    return {"token": sign_ws_token(settings.secret_key, str(user.id), 60), "expires_in": 60}


@router.get("/sessions")
async def my_sessions(found: tuple[AuthSession, User] = Depends(deps.current_session),
                      db: AsyncSession = Depends(get_db)):
    current, user = found
    rows = await repo.list_auth_sessions(db, user.id)
    return {"sessions": [{"id": str(s.id), "kind": s.kind, "label": s.label,
                          "created_at": s.created_at.isoformat(),
                          "last_seen_at": s.last_seen_at.isoformat(),
                          "expires_at": s.expires_at.isoformat(), "user_agent": s.user_agent,
                          "current": s.id == current.id} for s in rows]}


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: str, user: User = Depends(deps.current_user),
                         db: AsyncSession = Depends(get_db)):
    for s in await repo.list_auth_sessions(db, user.id):
        if str(s.id) == session_id:
            await repo.delete_auth_session(db, s.id)
            await repo.audit(db, user, "session.revoked", "auth_session", session_id)
            await db.commit()
            return Response(status_code=204)
    raise HTTPException(404, "no such session")


# ─── CLI sign-in (browser-mediated) ───────────────────────────────────────────


class CliStart(BaseModel):
    label: str | None = None


@router.post("/cli/start")
async def cli_start(body: CliStart, db: AsyncSession = Depends(get_db),
                    settings: Settings = Depends(get_settings)):
    row = await repo.create_cli_code(db, (body.label or "")[:200] or None)
    await db.commit()
    return {"code": row.code,
            "verify_url": f"{settings.public_base}/api/auth/cli?code={quote(row.code)}",
            "expires_in": 600, "poll_interval": 3}


_CLI_PAGE = """<!doctype html><meta charset="utf-8"><title>Authorise swarn CLI</title>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;line-height:1.5">
<h2>Authorise a command-line sign-in</h2>
<p>{label}A terminal is asking to act as <b>{email}</b>. Only continue if you started this.</p>
<form method="post" action="/api/auth/cli/approve">
<input type="hidden" name="code" value="{code}">
<button style="padding:.6rem 1.2rem;font-size:1rem">Approve</button></form></body>"""


@router.get("/cli", response_class=HTMLResponse)
async def cli_page(request: Request, code: str, db: AsyncSession = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    row = await repo.get_cli_code(db, code)
    if row is None or row.status != "pending":
        return HTMLResponse("<p>This sign-in code is unknown or has expired. Run the command again.</p>",
                            status_code=404)
    found = await deps.resolve_session(db, settings, request.cookies.get(settings.cookie_name))
    if found is None:
        if settings.google_enabled:
            return RedirectResponse(f"/api/auth/google/login?cli_code={quote(code)}", status_code=302)
        raise HTTPException(401, "sign in first")
    _, user = found
    label = f"<i>{row.label}</i>: " if row.label else ""
    return HTMLResponse(_CLI_PAGE.format(label=label, email=user.email, code=code))


@router.post("/cli/approve", response_class=HTMLResponse)
async def cli_approve(request: Request, db: AsyncSession = Depends(get_db),
                      settings: Settings = Depends(get_settings)):
    form = await request.form()
    code = str(form.get("code") or "")
    if not deps.origin_allowed(request, settings):
        raise HTTPException(403, "bad origin")
    found = await deps.resolve_session(db, settings, request.cookies.get(settings.cookie_name))
    if found is None:
        raise HTTPException(401, "not signed in")
    _, user = found
    row = await repo.get_cli_code(db, code)
    if row is None or row.status != "pending":
        raise HTTPException(404, "code unknown or expired")
    _, token = await repo.create_auth_session(db, user, kind="cli",
                                              max_days=settings.cli_session_max_days,
                                              user_agent=row.label or "cli", ip=deps.client_ip(request),
                                              label=row.label)
    row.status = "approved"
    row.user_id = user.id
    row.token_plain = token
    await repo.audit(db, user, "cli.authorised", "cli_code", code, ip=deps.client_ip(request))
    await db.commit()
    return HTMLResponse("<p style='font-family:system-ui'>Done. You can close this tab and return "
                        "to the terminal.</p>")


class CliPoll(BaseModel):
    code: str


@router.post("/cli/poll")
async def cli_poll(body: CliPoll, db: AsyncSession = Depends(get_db)):
    row = await repo.get_cli_code(db, body.code)
    if row is None or row.status == "expired":
        raise HTTPException(404, "code unknown or expired")
    if row.status == "pending":
        return {"status": "pending"}
    if row.status == "approved" and row.token_plain:
        token, row.token_plain, row.status = row.token_plain, None, "claimed"
        user = await repo.get_user(db, row.user_id)
        await db.commit()
        return {"status": "approved", "token": token, "user": user_json(user) if user else None}
    raise HTTPException(410, "code already used")


_unused = (now_utc,)
