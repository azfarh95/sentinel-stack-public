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
        await db.commit()


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
