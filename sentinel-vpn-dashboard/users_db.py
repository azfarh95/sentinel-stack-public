"""SQLite-backed user / invite / revocation store for auth-perms v2.

Spec: metamcp-local/docs/auth-perms-v2.md §7.1

Single-tenant, single-process (the Suite container). All callers go
through the small helpers exported here — no raw SQL outside this
module. Uses stdlib sqlite3; no extra deps.

Tables:
  users         — beta users + their scope grants
  invites       — one-time redemption tokens (24h TTL by default)
  revocations   — jti blocklist for cookies that should no longer auth
  auth_events   — append-only audit log

DB path defaults to /data/auth.db inside the container (host-mounted
in compose). Override via SENTINEL_AUTH_DB env var (used by tests).

Concurrency: sqlite3 with WAL mode is fine for the Suite's request
rate (≤ a few QPS). All writes go through a single connection guarded
by a thread lock to avoid 'database is locked' under FastAPI's
threadpool.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


DB_PATH = Path(os.environ.get("SENTINEL_AUTH_DB", "/data/auth.db"))

_USER_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    handle          TEXT,
    scopes_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    revoked_at      TEXT,
    notes           TEXT,
    last_active_at  TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    token         TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    redeemed_at   TEXT,
    redeemed_ip   TEXT
);

CREATE INDEX IF NOT EXISTS idx_invites_user ON invites(user_id);

CREATE TABLE IF NOT EXISTS revocations (
    jti           TEXT PRIMARY KEY,
    user_id       TEXT,
    revoked_at    TEXT NOT NULL,
    reason        TEXT
);

CREATE TABLE IF NOT EXISTS auth_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    event         TEXT NOT NULL,
    user_id       TEXT,
    jti           TEXT,
    scopes_json   TEXT,
    ip            TEXT,
    user_agent    TEXT,
    payload_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON auth_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_user ON auth_events(user_id, ts DESC);
"""


def _iso_now() -> str:
    # ISO-8601 UTC, second precision. Sortable lexicographically.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init(db_path: Path | str | None = None) -> None:
    """Open the DB connection + apply the schema. Idempotent."""
    global _conn, DB_PATH
    if db_path is not None:
        DB_PATH = Path(db_path)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is not None:
            return
        c = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(SCHEMA)
        _conn = c


@contextmanager
def _cur():
    if _conn is None:
        raise RuntimeError("users_db.init() not called")
    with _lock:
        cur = _conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


def is_valid_user_id(uid: str) -> bool:
    return bool(_USER_ID_RE.match(uid or ""))


# ── users ────────────────────────────────────────────────────────────────────


def create_user(
    user_id: str,
    handle: str,
    scopes: list[str],
    expires_in_days: int | None = None,
    notes: str | None = None,
) -> dict:
    if not is_valid_user_id(user_id):
        raise ValueError(f"invalid user_id: {user_id!r}")
    if user_id == "owner":
        raise ValueError("reserved user_id 'owner'")
    now = _iso_now()
    exp: str | None = None
    if expires_in_days is not None and expires_in_days > 0:
        exp = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + expires_in_days * 86400),
        )
    scopes_json = json.dumps(sorted(set(scopes)), separators=(",", ":"))
    with _cur() as cur:
        cur.execute(
            "INSERT INTO users (id, handle, scopes_json, created_at, expires_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, handle, scopes_json, now, exp, notes),
        )
    return get_user(user_id)  # type: ignore[return-value]


def get_user(user_id: str) -> dict | None:
    with _cur() as cur:
        row = cur.execute(
            "SELECT id, handle, scopes_json, created_at, expires_at, "
            "revoked_at, notes, last_active_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["scopes"] = json.loads(d.pop("scopes_json") or "[]")
    return d


def list_users() -> list[dict]:
    with _cur() as cur:
        rows = cur.execute(
            "SELECT id, handle, scopes_json, created_at, expires_at, "
            "revoked_at, notes, last_active_at FROM users "
            "ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scopes"] = json.loads(d.pop("scopes_json") or "[]")
        out.append(d)
    return out


def update_scopes(user_id: str, scopes: list[str]) -> None:
    scopes_json = json.dumps(sorted(set(scopes)), separators=(",", ":"))
    with _cur() as cur:
        cur.execute("UPDATE users SET scopes_json=? WHERE id=?", (scopes_json, user_id))


def mark_active(user_id: str) -> None:
    with _cur() as cur:
        cur.execute("UPDATE users SET last_active_at=? WHERE id=?", (_iso_now(), user_id))


def revoke_user(user_id: str, reason: str | None = None) -> int:
    """Mark user revoked AND add every active (non-redeemed-expired) jti
    we've ever minted for them to the revocation list. Returns count of
    jtis revoked. Idempotent."""
    now = _iso_now()
    with _cur() as cur:
        cur.execute("UPDATE users SET revoked_at=? WHERE id=?", (now, user_id))
        # Find all jtis we've ever issued (from auth_events 'cookie.issue')
        rows = cur.execute(
            "SELECT DISTINCT jti FROM auth_events "
            "WHERE event='cookie.issue' AND user_id=? AND jti IS NOT NULL AND jti<>''",
            (user_id,),
        ).fetchall()
        count = 0
        for r in rows:
            jti = r["jti"]
            # INSERT OR IGNORE — idempotent
            cur.execute(
                "INSERT OR IGNORE INTO revocations (jti, user_id, revoked_at, reason) "
                "VALUES (?, ?, ?, ?)",
                (jti, user_id, now, reason or "user revoked"),
            )
            count += cur.rowcount or 0
    return count


# ── invites ──────────────────────────────────────────────────────────────────


def create_invite(user_id: str, ttl_seconds: int = 24 * 3600) -> dict:
    if not get_user(user_id):
        raise ValueError(f"user not found: {user_id}")
    token = secrets.token_urlsafe(32)
    now_unix = int(time.time())
    created = _iso_now()
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_unix + ttl_seconds))
    with _cur() as cur:
        cur.execute(
            "INSERT INTO invites (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, created, expires),
        )
    return {"token": token, "user_id": user_id, "created_at": created, "expires_at": expires}


def consume_invite(token: str, ip: str | None) -> dict | None:
    """Single-use redemption. Returns the invite row if valid + redeems
    it atomically. Returns None for unknown / expired / already-used
    tokens (no enumeration — same null for all failure modes)."""
    now = _iso_now()
    with _cur() as cur:
        row = cur.execute(
            "SELECT token, user_id, expires_at, redeemed_at FROM invites WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["redeemed_at"] is not None:
            return None
        if row["expires_at"] < now:
            return None
        cur.execute(
            "UPDATE invites SET redeemed_at=?, redeemed_ip=? WHERE token=? AND redeemed_at IS NULL",
            (now, ip, token),
        )
        if cur.rowcount != 1:
            return None
    return {"token": token, "user_id": row["user_id"], "redeemed_at": now}


def list_invites_for_user(user_id: str) -> list[dict]:
    with _cur() as cur:
        rows = cur.execute(
            "SELECT token, created_at, expires_at, redeemed_at, redeemed_ip "
            "FROM invites WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_pending_invites() -> int:
    now = _iso_now()
    with _cur() as cur:
        row = cur.execute(
            "SELECT COUNT(*) AS n FROM invites WHERE redeemed_at IS NULL AND expires_at > ?",
            (now,),
        ).fetchone()
    return int(row["n"]) if row else 0


# ── revocations ──────────────────────────────────────────────────────────────


def is_revoked(jti: str) -> bool:
    if not jti:
        return False
    with _cur() as cur:
        row = cur.execute("SELECT 1 FROM revocations WHERE jti=?", (jti,)).fetchone()
    return row is not None


# ── audit log ────────────────────────────────────────────────────────────────


def log_event(
    event: str,
    user_id: str | None = None,
    jti: str | None = None,
    scopes: list[str] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    payload: dict | None = None,
) -> None:
    scopes_json = json.dumps(scopes) if scopes is not None else None
    payload_json = json.dumps(payload) if payload else None
    with _cur() as cur:
        cur.execute(
            "INSERT INTO auth_events "
            "(ts, event, user_id, jti, scopes_json, ip, user_agent, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_iso_now(), event, user_id, jti, scopes_json, ip, user_agent, payload_json),
        )


def recent_events(limit: int = 200, user_id: str | None = None) -> list[dict]:
    with _cur() as cur:
        if user_id is not None:
            rows = cur.execute(
                "SELECT id, ts, event, user_id, jti, scopes_json, ip, user_agent, payload_json "
                "FROM auth_events WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT id, ts, event, user_id, jti, scopes_json, ip, user_agent, payload_json "
                "FROM auth_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        sj = d.pop("scopes_json")
        d["scopes"] = json.loads(sj) if sj else None
        pj = d.pop("payload_json")
        d["payload"] = json.loads(pj) if pj else None
        out.append(d)
    return out
