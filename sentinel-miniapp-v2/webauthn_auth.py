"""WebAuthn / passkey support for the Sentinel Suite Mini App (Phase 4).

Adds passkey login as an alternative to the Telegram-identity + TOTP flow.
This is a single-owner app, so every credential belongs to OWNER_ID — there is
no multi-user table. Credentials live in the same sessions.db SQLite file the
bridge already uses; this module owns its own `webauthn_credentials` table and
a thread-local connection so it stays self-contained.

Trust model:
  * Registration is gated by an EXISTING owner session (the bridge enforces the
    session-token middleware on the register/* routes). You can only add a
    passkey while already logged in.
  * Authentication is pre-session (gated only by the page's sentinel token, like
    the Telegram/TOTP endpoints). A successful assertion lets the bridge mint a
    normal session token for OWNER_ID.
  * The bearer secret never exists here — only the credential's COSE public key,
    which is useless without the private key held in the authenticator.

RP config is environment-driven so it can track the deployment domain without a
code change:
  WEBAUTHN_RP_ID    registrable domain, suffix of every allowed origin
                    (default "your-domain.example.com" so any *.your-domain.example.com works)
  WEBAUTHN_RP_NAME  human label shown by the authenticator
  WEBAUTHN_ORIGINS  comma-separated full origins accepted during verification
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import secrets as _secrets
from typing import Any

from webauthn import (
    generate_registration_options,
    generate_authentication_options,
    verify_registration_response,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

_CHALLENGE_TTL = 5 * 60  # seconds a pending ceremony challenge stays valid


class WebAuthnManager:
    def __init__(self, *, db_path: str, owner_id: int,
                 rp_id: str | None = None, rp_name: str | None = None,
                 origins: list[str] | None = None,
                 user_name: str = "owner") -> None:
        self.db_path = db_path
        self.owner_id = owner_id
        self.rp_id = rp_id or os.environ.get("WEBAUTHN_RP_ID", "your-domain.example.com")
        self.rp_name = rp_name or os.environ.get("WEBAUTHN_RP_NAME", "Sentinel Suite")
        env_origins = os.environ.get("WEBAUTHN_ORIGINS", "")
        self.origins = origins or (
            [o.strip() for o in env_origins.split(",") if o.strip()]
            or [
                "https://your-domain.example.com",
                "https://media.your-domain.example.com",
            ]
        )
        # Stable per-owner WebAuthn user handle (bytes). Single owner, so a
        # deterministic handle derived from owner_id is fine.
        self.user_handle = f"sentinel-owner-{owner_id}".encode()
        self.user_name = user_name
        self._local = threading.local()
        self._wlock = threading.Lock()
        # Pending ceremony challenges, keyed by an opaque handle the client
        # echoes back on verify. {handle: {"challenge": bytes, "kind": str, "exp": float}}
        self._pending: dict[str, dict] = {}
        self._plock = threading.Lock()

    # ── storage ──────────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "c"):
            c = sqlite3.connect(self.db_path, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS webauthn_credentials (
                    credential_id TEXT PRIMARY KEY,
                    public_key    BLOB    NOT NULL,
                    sign_count    INTEGER NOT NULL DEFAULT 0,
                    transports    TEXT    NOT NULL DEFAULT '',
                    label         TEXT    NOT NULL DEFAULT '',
                    aaguid        TEXT    NOT NULL DEFAULT '',
                    device_type   TEXT    NOT NULL DEFAULT '',
                    backed_up     INTEGER NOT NULL DEFAULT 0,
                    created_at    REAL    NOT NULL,
                    last_used_at  REAL
                )
                """
            )
            c.commit()
            self._local.c = c
        return self._local.c

    def _all_descriptors(self) -> list[PublicKeyCredentialDescriptor]:
        rows = self._conn().execute(
            "SELECT credential_id, transports FROM webauthn_credentials"
        ).fetchall()
        out = []
        for cred_id_b64, transports in rows:
            try:
                out.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred_id_b64)))
            except Exception:
                continue
        return out

    def has_credentials(self) -> bool:
        return bool(
            self._conn().execute(
                "SELECT 1 FROM webauthn_credentials LIMIT 1"
            ).fetchone()
        )

    def list_credentials(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT credential_id, label, aaguid, device_type, backed_up, "
            "created_at, last_used_at FROM webauthn_credentials ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "label": r[1] or "Passkey",
                "aaguid": r[2],
                "device_type": r[3],
                "backed_up": bool(r[4]),
                "created_at": r[5],
                "last_used_at": r[6],
            }
            for r in rows
        ]

    def delete_credential(self, cred_id_b64: str) -> bool:
        with self._wlock:
            cur = self._conn().execute(
                "DELETE FROM webauthn_credentials WHERE credential_id = ?", (cred_id_b64,)
            )
            self._conn().commit()
        return cur.rowcount > 0

    # ── challenge bookkeeping ────────────────────────────────────────────────
    def _stash_challenge(self, challenge: bytes, kind: str) -> str:
        handle = _secrets.token_hex(16)
        now = time.time()
        with self._plock:
            # opportunistic prune
            for k in [k for k, v in self._pending.items() if v["exp"] < now]:
                self._pending.pop(k, None)
            self._pending[handle] = {"challenge": challenge, "kind": kind,
                                     "exp": now + _CHALLENGE_TTL}
        return handle

    def _take_challenge(self, handle: str, kind: str) -> bytes | None:
        with self._plock:
            rec = self._pending.pop(handle, None)
        if not rec or rec["kind"] != kind or rec["exp"] < time.time():
            return None
        return rec["challenge"]

    # ── registration ─────────────────────────────────────────────────────────
    def registration_options(self) -> tuple[dict, str]:
        opts = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self.rp_name,
            user_id=self.user_handle,
            user_name=self.user_name,
            user_display_name="Sentinel Owner",
            exclude_credentials=self._all_descriptors(),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
        )
        handle = self._stash_challenge(opts.challenge, "register")
        import json as _json
        return _json.loads(options_to_json(opts)), handle

    def registration_verify(self, handle: str, credential: dict, label: str) -> dict:
        challenge = self._take_challenge(handle, "register")
        if challenge is None:
            raise ValueError("challenge_expired")
        v = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.rp_id,
            expected_origin=self.origins,
            require_user_verification=False,
        )
        cred_id_b64 = bytes_to_base64url(v.credential_id)
        transports = ""
        try:
            t = (credential.get("response") or {}).get("transports")
            if isinstance(t, list):
                transports = ",".join(str(x) for x in t)
        except Exception:
            pass
        with self._wlock:
            self._conn().execute(
                "INSERT OR REPLACE INTO webauthn_credentials "
                "(credential_id, public_key, sign_count, transports, label, aaguid, "
                " device_type, backed_up, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    cred_id_b64,
                    v.credential_public_key,
                    v.sign_count,
                    transports,
                    (label or "Passkey")[:60],
                    v.aaguid or "",
                    getattr(v.credential_device_type, "value", str(v.credential_device_type or "")),
                    1 if v.credential_backed_up else 0,
                    time.time(),
                ),
            )
            self._conn().commit()
        return {"id": cred_id_b64, "label": (label or "Passkey")[:60]}

    # ── authentication ───────────────────────────────────────────────────────
    def authentication_options(self) -> tuple[dict, str]:
        opts = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=self._all_descriptors(),
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        handle = self._stash_challenge(opts.challenge, "auth")
        import json as _json
        return _json.loads(options_to_json(opts)), handle

    def authentication_verify(self, handle: str, credential: dict) -> bool:
        challenge = self._take_challenge(handle, "auth")
        if challenge is None:
            return False
        raw_id = credential.get("rawId") or credential.get("id")
        if not raw_id:
            return False
        try:
            cred_id_b64 = bytes_to_base64url(base64url_to_bytes(raw_id))
        except Exception:
            return False
        row = self._conn().execute(
            "SELECT public_key, sign_count FROM webauthn_credentials WHERE credential_id = ?",
            (cred_id_b64,),
        ).fetchone()
        if not row:
            return False
        public_key, sign_count = row
        try:
            v = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.origins,
                credential_public_key=public_key,
                credential_current_sign_count=sign_count,
                require_user_verification=False,
            )
        except Exception:
            return False
        with self._wlock:
            self._conn().execute(
                "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = ? "
                "WHERE credential_id = ?",
                (v.new_sign_count, time.time(), cred_id_b64),
            )
            self._conn().commit()
        return True
