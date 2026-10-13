"""Google sign-in as plain OpenID Connect: discovery, authorization-code flow
with PKCE, ID-token verification against Google's JWKS.

Only the issuer, client id and secret are Google-specific, so another OIDC
provider (Keycloak, Entra) is a configuration change, not a rewrite.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class OIDCError(Exception):
    pass


class GoogleOIDC:
    def __init__(self, client_id: str, client_secret: str, discovery_url: str = DISCOVERY_URL):
        self.client_id = client_id
        self.client_secret = client_secret
        self.discovery_url = discovery_url
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._jwks: PyJWKClient | None = None

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None or time.time() - self._discovery_at > 6 * 3600:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(self.discovery_url)
                r.raise_for_status()
                self._discovery = r.json()
                self._discovery_at = time.time()
                self._jwks = PyJWKClient(self._discovery["jwks_uri"], cache_keys=True)
        return self._discovery

    async def auth_url(self, *, redirect_uri: str, state: str, code_challenge: str,
                       hd: str | None = None) -> str:
        disc = await self.discovery()
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "online",
            "prompt": "select_account",
        }
        if hd:
            params["hd"] = hd
        return f"{disc['authorization_endpoint']}?{urlencode(params)}"

    async def exchange(self, *, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        disc = await self.discovery()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(disc["token_endpoint"], data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code_verifier": code_verifier,
            })
        if r.status_code != 200:
            raise OIDCError(f"token exchange failed: {r.status_code} {r.text[:200]}")
        return r.json()

    async def verify_id_token(self, id_token: str) -> dict[str, Any]:
        """Signature, issuer, audience, expiry. Returns the claims."""
        await self.discovery()
        assert self._jwks is not None
        try:
            key = self._jwks.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(id_token, key.key, algorithms=["RS256"], audience=self.client_id,
                                options={"require": ["exp", "iat", "sub", "email"]})
        except jwt.PyJWTError as e:
            raise OIDCError(f"invalid id token: {e}") from e
        if claims.get("iss") not in ISSUERS:
            raise OIDCError("unexpected issuer")
        return claims


def email_allowed(email: str, hd: str | None, *, domains: list[str], emails: list[str]) -> bool:
    """The allow rules from section 9.1. With neither list set, everyone
    Google verifies is allowed (fine for a laptop, not for a shared host)."""
    email = email.lower()
    if emails and email in emails:
        return True
    if domains:
        domain = email.rsplit("@", 1)[-1]
        if domain in domains or (hd and hd.lower() in domains):
            return True
        return False
    return not emails
