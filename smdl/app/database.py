import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")


async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS url_cache (
                url        TEXT PRIMARY KEY,
                files      TEXT NOT NULL,
                platform   TEXT,
                uploader   TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Per-user download history. url_cache stays a global content cache;
        # this table is the audit trail of who-downloaded-what.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                url           TEXT NOT NULL,
                files         TEXT NOT NULL,
                platform      TEXT,
                uploader      TEXT,
                downloaded_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_dh_chat_time
            ON download_history (chat_id, downloaded_at DESC)
        """)
        # Users directory — populated implicitly on first interaction by
        # auth.record_interaction(). Status drives the gate. New users land
        # as 'pending' and need owner approval before they're 'active'.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id            INTEGER PRIMARY KEY,
                username           TEXT,
                first_name         TEXT,
                last_name          TEXT,
                status             TEXT NOT NULL DEFAULT 'pending',
                first_seen         TEXT NOT NULL,
                last_seen          TEXT NOT NULL,
                interaction_count  INTEGER NOT NULL DEFAULT 0,
                banned_at          TEXT,
                banned_reason      TEXT,
                pending_code       TEXT,
                pending_expires_at TEXT
            )
        """)
        # Idempotent column adds for upgraders (SQLite is permissive about
        # ALTER ADD if the column doesn't exist yet — but it errors on dup,
        # so we ask the schema first).
        async with db.execute("PRAGMA table_info(users)") as cur:
            cols = {row[1] async for row in cur}
        if "pending_code" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN pending_code TEXT")
        if "pending_expires_at" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN pending_expires_at TEXT")
        # Approved groups — chat_ids of Telegram groups the owner has trusted.
        # Members of these groups can use the bot WITHOUT per-user approval.
        # Trade-off: bot replies are visible to the whole group; download
        # history attributes to the group's chat_id (shared by all members).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS approved_groups (
                chat_id     INTEGER PRIMARY KEY,
                label       TEXT,
                approved_by INTEGER NOT NULL,
                approved_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def is_group_approved(chat_id: int) -> bool:
    """Fast lookup used by the auth gate. Expects a negative chat_id (groups)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM approved_groups WHERE chat_id = ? LIMIT 1",
            (int(chat_id),),
        ) as cur:
            return (await cur.fetchone()) is not None


async def list_approved_groups() -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM approved_groups ORDER BY approved_at DESC"
        ) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def approve_group(chat_id: int, label: str | None,
                         approved_by: int) -> bool:
    """Insert (or refresh label for) an approved group. Refuses positive
    chat_ids — those are DMs and use the per-user flow."""
    if int(chat_id) >= 0:
        return False
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO approved_groups (chat_id, label, approved_by, approved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                label = excluded.label
        """, (int(chat_id), label, int(approved_by), now))
        await db.commit()
        return True


async def unapprove_group(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM approved_groups WHERE chat_id = ?",
            (int(chat_id),),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


import secrets as _secrets
from datetime import timedelta as _timedelta


PENDING_CODE_TTL = _timedelta(minutes=1)


def _generate_approval_code() -> str:
    """Cryptographically-random 9-digit code, hyphen-grouped for readability.
    Format: '123-456-789'. ~10^9 entropy — easily brute-forceable in pure
    isolation, but the gate is also chat_id-bound, single-use, and 24h-TTL."""
    n = _secrets.randbelow(10**9)
    s = f"{n:09d}"
    return f"{s[0:3]}-{s[3:6]}-{s[6:9]}"


def _norm_code(code: str) -> str:
    """Strip hyphens/spaces, accept either '123456789' or '123-456-789'."""
    return "".join(c for c in (code or "") if c.isdigit())


def _is_owner_chat(chat_id: int) -> bool:
    """Owner check that doesn't import auth.py (which imports us). Reads the
    config module's OWNER_CHAT_ID directly. Used for fast-pathing owner row
    creation to 'active' so it never appears in pending lists."""
    try:
        from .config import OWNER_CHAT_ID
        return OWNER_CHAT_ID is not None and int(chat_id) == int(OWNER_CHAT_ID)
    except Exception:
        return False


async def record_interaction(chat_id: int, username: str | None = None,
                              first_name: str | None = None,
                              last_name: str | None = None) -> dict:
    """UPSERT a user row on every bot interaction. Returns the post-update row.

    New users land as 'pending' with a fresh 9-digit code (1-min TTL).
    EXCEPT the owner — owner rows are always created as 'active' with no
    code, so they never surface in the Admin pending list.

    Existing-pending users get a NEW code if the old one expired; otherwise
    the existing one is preserved. Active/banned rows are never auto-flipped
    here — only the owner can promote/demote via admin endpoints."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    is_owner = _is_owner_chat(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
        if row is None:
            if is_owner:
                await db.execute("""
                    INSERT INTO users
                        (chat_id, username, first_name, last_name,
                         status, first_seen, last_seen, interaction_count)
                    VALUES (?, ?, ?, ?, 'active', ?, ?, 1)
                """, (int(chat_id), username, first_name, last_name, now, now))
                await db.commit()
                return {"chat_id": int(chat_id), "username": username,
                        "first_name": first_name, "last_name": last_name,
                        "status": "active", "first_seen": now, "last_seen": now,
                        "interaction_count": 1, "banned_at": None,
                        "banned_reason": None, "pending_code": None,
                        "pending_expires_at": None}
            code = _generate_approval_code()
            expiry = (now_dt + PENDING_CODE_TTL).isoformat()
            await db.execute("""
                INSERT INTO users
                    (chat_id, username, first_name, last_name,
                     status, first_seen, last_seen, interaction_count,
                     pending_code, pending_expires_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, 1, ?, ?)
            """, (int(chat_id), username, first_name, last_name, now, now, code, expiry))
            await db.commit()
            return {"chat_id": int(chat_id), "username": username,
                    "first_name": first_name, "last_name": last_name,
                    "status": "pending", "first_seen": now, "last_seen": now,
                    "interaction_count": 1, "banned_at": None, "banned_reason": None,
                    "pending_code": code, "pending_expires_at": expiry}
        # Existing row — refresh contact info, bump counters.
        await db.execute("""
            UPDATE users SET
                username          = COALESCE(?, username),
                first_name        = COALESCE(?, first_name),
                last_name         = COALESCE(?, last_name),
                last_seen         = ?,
                interaction_count = interaction_count + 1
            WHERE chat_id = ?
        """, (username, first_name, last_name, now, int(chat_id)))
        # If pending and code expired, rotate to a new code.
        if (row["status"] or "").lower() == "pending":
            expiry_str = row["pending_expires_at"] or ""
            try:
                expired = (not expiry_str) or datetime.fromisoformat(expiry_str) < now_dt
            except Exception:
                expired = True
            if expired:
                new_code = _generate_approval_code()
                new_expiry = (now_dt + PENDING_CODE_TTL).isoformat()
                await db.execute("""
                    UPDATE users SET pending_code = ?, pending_expires_at = ?
                    WHERE chat_id = ?
                """, (new_code, new_expiry, int(chat_id)))
        await db.commit()
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}


async def rotate_pending_code(chat_id: int) -> dict | None:
    """Force-generate a fresh approval code for a pending user. Returns the
    updated row, or None if the user isn't in 'pending' state (active users
    are already approved; banned users must stay banned). Used by the
    /regenerate_token bot command when an old code expired."""
    now_dt = datetime.now(timezone.utc)
    new_code = _generate_approval_code()
    new_expiry = (now_dt + PENDING_CODE_TTL).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            UPDATE users SET pending_code = ?, pending_expires_at = ?
            WHERE chat_id = ? AND status = 'pending'
        """, (new_code, new_expiry, int(chat_id)))
        await db.commit()
        if (cur.rowcount or 0) == 0:
            return None
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def find_user_by_pending_code(code: str) -> dict | None:
    """Look up a user by their pending approval code. Returns None if not
    found, expired, or already approved."""
    normalized = _norm_code(code)
    if len(normalized) != 9:
        return None
    formatted = f"{normalized[0:3]}-{normalized[3:6]}-{normalized[6:9]}"
    now_dt = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE status = 'pending' AND pending_code = ?",
            (formatted,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        # Expiry check (rotation happens on next /start, but a stale code
        # presented now must not silently approve).
        try:
            if row["pending_expires_at"] and \
               datetime.fromisoformat(row["pending_expires_at"]) < now_dt:
                return None
        except Exception:
            return None
        return dict(row)


async def approve_user(chat_id: int) -> bool:
    """Flip a user to 'active', clear the pending code. Idempotent on
    already-active rows. Refuses to operate on banned rows — admin must
    explicitly unban first."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            UPDATE users SET status = 'active',
                             pending_code = NULL,
                             pending_expires_at = NULL
            WHERE chat_id = ?
              AND status IN ('pending', 'active')
        """, (int(chat_id),))
        await db.commit()
        return (cur.rowcount or 0) > 0


async def get_user(chat_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_users() -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM users
            ORDER BY (status='banned') DESC, last_seen DESC
        """) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def set_user_status(chat_id: int, status: str, reason: str | None = None) -> bool:
    """Flip status to 'active' or 'banned'. Returns True if a row was updated."""
    if status not in ("active", "banned"):
        return False
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "banned":
            cur = await db.execute("""
                UPDATE users SET status = 'banned', banned_at = ?, banned_reason = ?
                WHERE chat_id = ?
            """, (now, reason, int(chat_id)))
        else:
            cur = await db.execute("""
                UPDATE users SET status = 'active', banned_at = NULL, banned_reason = NULL
                WHERE chat_id = ?
            """, (int(chat_id),))
        await db.commit()
        return (cur.rowcount or 0) > 0


def _normalise_url(url: str) -> str:
    return url.strip().rstrip("/")


async def get_url_cache(url: str) -> dict | None:
    """Return cached entry only if URL was downloaded before AND all files still exist on disk."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM url_cache WHERE url = ?", (_normalise_url(url),)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
    try:
        files = json.loads(d.get("files") or "[]")
    except Exception:
        return None
    if not files:
        return None
    if not all(Path(f).exists() for f in files):
        return None
    d["files"] = files
    return d


async def set_url_cache(url: str, files: list[str], platform: str | None, uploader: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO url_cache (url, files, platform, uploader, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                files      = excluded.files,
                platform   = excluded.platform,
                uploader   = excluded.uploader,
                created_at = excluded.created_at
        """, (
            _normalise_url(url),
            json.dumps(files),
            platform,
            uploader,
            datetime.now(timezone.utc).isoformat(),
        ))
        await db.commit()


async def cache_stats() -> dict:
    """Return {count, oldest, newest} for the URL cache."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM url_cache"
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return {"count": 0, "oldest": None, "newest": None}
    return {"count": row[0], "oldest": row[1], "newest": row[2]}


async def clear_cache(url: str | None = None) -> int:
    """Clear cache. If url given, only that entry; otherwise everything.
    Returns the count of rows removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        if url is None:
            cur = await db.execute("DELETE FROM url_cache")
        else:
            cur = await db.execute("DELETE FROM url_cache WHERE url = ?", (_normalise_url(url),))
        await db.commit()
        return cur.rowcount or 0


async def record_download(chat_id: int, url: str, files: list[str],
                          platform: str | None, uploader: str | None) -> None:
    """Append a row to download_history. Never raises — failures are swallowed
    upstream (this is audit/telemetry, not load-bearing for the user flow)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO download_history
                (chat_id, url, files, platform, uploader, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            int(chat_id),
            _normalise_url(url),
            json.dumps(files),
            platform,
            uploader,
            datetime.now(timezone.utc).isoformat(),
        ))
        await db.commit()


async def list_download_history(chat_id: int, limit: int = 50) -> list[dict]:
    """Return the most recent `limit` downloads for a specific chat_id, newest first.
    Each row: {url, files (list), platform, uploader, downloaded_at}."""
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT url, files, platform, uploader, downloaded_at
            FROM download_history
            WHERE chat_id = ?
            ORDER BY downloaded_at DESC
            LIMIT ?
        """, (int(chat_id), int(limit))) as cur:
            async for row in cur:
                d = dict(row)
                try: d["files"] = json.loads(d.get("files") or "[]")
                except Exception: d["files"] = []
                out.append(d)
    return out


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        await db.commit()
