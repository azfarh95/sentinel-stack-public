"""Sentinel auth-perms v2 — shared cookie parser + scope enforcement.

Spec: metamcp-local/docs/auth-perms-v2.md

Each pillar copies this file verbatim (no shared PyPI package — pillars
deploy independently). Drop-in module: imports stdlib only, exports
three functions. The pillar's existing auth module (e.g. miniapp.py in
SMDL) calls these from within its own _verify() implementation.

Cookie formats supported:
  v1 (legacy, owner-only):   <ts>.<nonce>.<hmac>
                              → user_id=owner, scopes=["*"]
  v2 (scoped beta users):    v2.<ts>.<user_id>.<jti>.<scopes_b64>.<hmac>
                              → user_id=slug, scopes=[…parsed list…]

Caller passes the cookie value + the shared HMAC secret
(OWNER_AUTH_TOKEN from .env.local). Returns a normalised payload dict
or raises HTTPException.

Public surface:
  parse_session_cookie(raw, secret) -> dict | raises HTTPException
  has_scope(payload, required)       -> bool
  require_scope(payload, required)   -> None | raises HTTPException(403)
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256

from fastapi import HTTPException


# Per spec §4. Phase 2 may override V2_MAX_AGE_SEC per-user by baking
# the effective expiry into the cookie's iat (so verifier doesn't
# need to query the user table per request).
V1_MAX_AGE_SEC = 90 * 24 * 3600
V2_MAX_AGE_SEC = 90 * 24 * 3600


def parse_session_cookie(raw: str, secret: str) -> dict:
    """Parse + verify a session cookie. Returns a payload dict with:
        version  : "v1" | "v2"
        user_id  : "owner" (v1) or slug (v2)
        scopes   : list[str] — ["*"] for v1, parsed array for v2
        jti      : random per-cookie id (v2 only; "" for v1)
        iat      : issuance unix-ts
        expired  : bool — True if older than the version's max age
    Raises HTTPException(401) on missing/malformed/wrong-signature input.
    Does NOT raise on `expired`; callers decide whether to honour it
    (typically they treat expired as 401, but the design leaves the
    knob exposed)."""
    if not raw or not secret:
        raise HTTPException(401, "no session")
    # Detect v2 first by literal prefix — cheap, no regex.
    if raw.startswith("v2."):
        return _parse_v2(raw, secret)
    return _parse_v1(raw, secret)


def _parse_v1(raw: str, secret: str) -> dict:
    parts = raw.split(".")
    if len(parts) != 3:
        raise HTTPException(401, "unrecognised cookie format")
    ts_s, nonce, sig = parts
    body = f"{ts_s}.{nonce}"
    expected = hmac.new(secret.encode(), body.encode(), sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "bad signature")
    try:
        ts = int(ts_s)
    except ValueError:
        raise HTTPException(401, "malformed v1 cookie")
    return {
        "version": "v1",
        "user_id": "owner",
        "scopes":  ["*"],
        "jti":     "",
        "iat":     ts,
        "expired": (time.time() - ts) >= V1_MAX_AGE_SEC,
    }


def _parse_v2(raw: str, secret: str) -> dict:
    parts = raw.split(".")
    # v2.<ts>.<uid>.<jti>.<scopes_b64>.<sig>  →  6 parts
    if len(parts) != 6 or parts[0] != "v2":
        raise HTTPException(401, "unrecognised v2 cookie format")
    _, ts_s, user_id, jti, scopes_b64, sig = parts
    body = ".".join(parts[:5])
    expected = hmac.new(secret.encode(), body.encode(), sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "bad signature")
    try:
        ts = int(ts_s)
        # urlsafe_b64decode tolerates missing padding when fed `+ '=='`.
        scopes_json = base64.urlsafe_b64decode(scopes_b64 + "==").decode("utf-8")
        scopes = json.loads(scopes_json)
        if not isinstance(scopes, list):
            raise ValueError("scopes must be a list")
        scopes = [str(s) for s in scopes]
    except Exception:
        raise HTTPException(401, "malformed v2 cookie payload")
    return {
        "version": "v2",
        "user_id": user_id,
        "scopes":  scopes,
        "jti":     jti,
        "iat":     ts,
        "expired": (time.time() - ts) >= V2_MAX_AGE_SEC,
    }


def has_scope(payload: dict, required: str) -> bool:
    """True if the payload's scopes grant `required`. Handles:
      • '*' wildcard — anything
      • '<pillar>.*' wildcard — any scope inside that pillar
      • exact match
    """
    scopes = payload.get("scopes") or []
    if "*" in scopes:
        return True
    if required in scopes:
        return True
    # Pillar-wide wildcard: "smdl.*" grants "smdl.iptv", "smdl.downloader", …
    if "." in required:
        pillar_wildcard = required.split(".", 1)[0] + ".*"
        if pillar_wildcard in scopes:
            return True
    return False


def require_scope(payload: dict, required: str) -> None:
    """Enforce. Raises HTTPException(403) if the scope is missing.
    No-op (returns None) if granted."""
    if not has_scope(payload, required):
        raise HTTPException(403, f"missing scope: {required}")


def issue_v2_cookie(
    secret: str,
    user_id: str,
    scopes: list[str],
    *,
    iat: int | None = None,
    jti: str | None = None,
) -> str:
    """Mint a v2 cookie. Used by the Suite admin in Phase 2; included
    here so pillars can also use it for unit tests / smoke fixtures
    without depending on the suite container."""
    import secrets as _secrets
    if iat is None:
        iat = int(time.time())
    if jti is None:
        jti = _secrets.token_urlsafe(16)
    scopes_json = json.dumps(scopes, separators=(",", ":"))
    scopes_b64 = base64.urlsafe_b64encode(scopes_json.encode("utf-8")).rstrip(b"=").decode("ascii")
    body = f"v2.{iat}.{user_id}.{jti}.{scopes_b64}"
    sig = hmac.new(secret.encode(), body.encode(), sha256).hexdigest()
    return f"{body}.{sig}"
