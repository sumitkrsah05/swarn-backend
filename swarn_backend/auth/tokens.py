"""Short-lived signed tokens (websocket handshake) and PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_ws_token(secret: str, user_id: str, ttl_s: int = 60) -> str:
    exp = int(time.time()) + ttl_s
    body = f"{user_id}.{exp}".encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


def verify_ws_token(secret: str, token: str) -> str | None:
    """The user id when the token is intact and unexpired, else None."""
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = _unb64(body_b64)
        sig = _unb64(sig_b64)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        user_id, exp = body.decode().rsplit(".", 1)
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return user_id


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for the S256 method."""
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def new_state() -> str:
    return _b64(secrets.token_bytes(32))
