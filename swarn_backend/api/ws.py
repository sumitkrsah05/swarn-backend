"""`WS /ws/live` — authenticated by the session cookie (same origin) or a
60-second token from `POST /api/auth/ws-token`; bound to one user."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from swarn_backend.auth.deps import resolve_session
from swarn_backend.auth.tokens import verify_ws_token
from swarn_backend.db.engine import session_factory
from swarn_backend.settings import get_settings
from swarn_backend.ws.manager import get_live

router = APIRouter(tags=["live"])


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    settings = get_settings()
    user_id: str | None = None
    role = "member"
    token = websocket.query_params.get("token")
    if token:
        user_id = verify_ws_token(settings.secret_key, token)
    if user_id is None:
        cookie = websocket.cookies.get(settings.cookie_name)
        auth = websocket.headers.get("authorization") or ""
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        async with session_factory()() as db:
            found = await resolve_session(db, settings, bearer or cookie)
            await db.commit()
        if found is not None:
            user_id = str(found[1].id)
            role = found[1].role
    if user_id is None:
        await websocket.close(code=4401)
        return
    firehose = role == "admin" and websocket.query_params.get("all") == "1"
    live = get_live()
    await live.connect(websocket, user_id, firehose=firehose)
    try:
        while True:
            await websocket.receive_text()      # "ping" keep-alives; nothing is expected
    except WebSocketDisconnect:
        pass
    finally:
        await live.disconnect(websocket, user_id)
