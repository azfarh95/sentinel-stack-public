import json
import os
import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # ── jobs ──────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id       TEXT PRIMARY KEY,
                url          TEXT NOT NULL,
                quality      TEXT NOT NULL,
                fmt          TEXT,
                status       TEXT NOT NULL DEFAULT 'queued',
                progress     TEXT,
                speed        TEXT,
                eta          TEXT,
                filename     TEXT,
                filepath     TEXT,
                download_dir TEXT,
                files        TEXT,
                error        TEXT,
                created_at   TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        # Migrate: add columns to existing DBs that predate this schema
        for col, definition in [("download_dir", "TEXT"), ("files", "TEXT")]:
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists

        # ── url_cache ─────────────────────────────────────────────────────────
        # Maps a URL to the files it produced so we can skip re-downloading.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS url_cache (
                url        TEXT PRIMARY KEY,
                files      TEXT NOT NULL,   -- JSON array of absolute container paths
                platform   TEXT,
                uploader   TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # ── settings ──────────────────────────────────────────────────────────
        # Simple key/value store for persistent configuration.
        # telegram_mode: "download" | "download+send"
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Default: download only (safe — no Telegram token required)
        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('telegram_mode', 'download')
        """)

        await db.commit()


# ── Jobs ──────────────────────────────────────────────────────────────────────

async def upsert_job(job: dict):
    files_json = json.dumps(job.get("files") or [])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO jobs
                (job_id, url, quality, fmt, status, progress, speed, eta,
                 filename, filepath, download_dir, files, error, created_at, completed_at)
            VALUES
                (:job_id, :url, :quality, :fmt, :status, :progress, :speed, :eta,
                 :filename, :filepath, :download_dir, :files_json, :error, :created_at, :completed_at)
            ON CONFLICT(job_id) DO UPDATE SET
                status       = excluded.status,
                progress     = excluded.progress,
                speed        = excluded.speed,
                eta          = excluded.eta,
                filename     = excluded.filename,
                filepath     = excluded.filepath,
                download_dir = excluded.download_dir,
                files        = excluded.files,
                error        = excluded.error,
                completed_at = excluded.completed_at
        """, {**job, "files_json": files_json})
        await db.commit()


async def get_job(job_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            # Deserialise files JSON back to list
            try:
                d["files"] = json.loads(d.get("files") or "[]")
            except Exception:
                d["files"] = []
            return d


async def get_recent_jobs(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for d in rows:
        try:
            d["files"] = json.loads(d.get("files") or "[]")
        except Exception:
            d["files"] = []
    return rows


# ── URL Cache ─────────────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    """Strip trailing slashes and lowercase the scheme+host for reliable cache hits."""
    return url.strip().rstrip("/")


async def get_url_cache(url: str) -> dict | None:
    """Return cached entry if URL was downloaded before AND all files still exist on disk."""
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
    # Only return cache hit if every file still exists on disk
    from pathlib import Path
    if not all(Path(f).exists() for f in files):
        return None
    d["files"] = files
    return d


async def set_url_cache(url: str, files: list[str], platform: str | None, uploader: str | None):
    from datetime import datetime, timezone
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


# ── Settings ──────────────────────────────────────────────────────────────────

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
