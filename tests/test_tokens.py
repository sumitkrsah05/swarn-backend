import time

from swarn_backend.auth.google import email_allowed
from swarn_backend.auth.tokens import pkce_pair, sign_ws_token, verify_ws_token
from swarn_backend.db.repo import hash_token


def test_ws_token_roundtrip():
    tok = sign_ws_token("secret", "user-1", ttl_s=60)
    assert verify_ws_token("secret", tok) == "user-1"
    assert verify_ws_token("other", tok) is None
    assert verify_ws_token("secret", tok + "x") is None


def test_ws_token_expires(monkeypatch):
    tok = sign_ws_token("secret", "user-1", ttl_s=1)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 5)
    assert verify_ws_token("secret", tok) is None


def test_pkce_shape():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128 and len(challenge) == 43


def test_hash_token_is_stable():
    assert hash_token("abc") == hash_token("abc") and len(hash_token("abc")) == 32


def test_email_allow_rules():
    assert email_allowed("a@esds.co.in", "esds.co.in", domains=["esds.co.in"], emails=[])
    assert not email_allowed("a@gmail.com", None, domains=["esds.co.in"], emails=[])
    assert email_allowed("a@gmail.com", None, domains=["esds.co.in"], emails=["a@gmail.com"])
    assert not email_allowed("b@gmail.com", None, domains=[], emails=["a@gmail.com"])
    assert email_allowed("anyone@x.io", None, domains=[], emails=[])
