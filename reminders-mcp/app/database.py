"""
SQLite metadata store for reminders.

APScheduler persists the job schedule itself; this table stores the
user-facing metadata (message, chat_id, human description, status).
The `id` column is always set to the APScheduler job id so they stay in sync.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = "/data/reminders.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id               TEXT PRIMARY KEY,
                chat_id          TEXT NOT NULL,
                message          TEXT NOT NULL,
                label            TEXT,
                recipients       TEXT,
                trigger_type     TEXT NOT NULL,
                trigger_description TEXT NOT NULL,
                when_raw         TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'active',
                created_at       TEXT NOT NULL,
                next_run         TEXT,
                last_run         TEXT
            )
        """)
        # Migration: add recipients column to existing DBs
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)").fetchall()}
        if "recipients" not in cols:
            conn.execute("ALTER TABLE reminders ADD COLUMN recipients TEXT")
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ── Write ──────────────────────────────────────────────────────────────────────

def create_reminder(
    reminder_id: str,
    chat_id: str,
    message: str,
    label: str | None,
    trigger_type: str,
    trigger_description: str,
    when_raw: str,
    next_run: str | None = None,
    recipients: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO reminders
                (id, chat_id, message, label, recipients, trigger_type, trigger_description,
                 when_raw, status, created_at, next_run, last_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
            """,
            (reminder_id, chat_id, message, label, recipients, trigger_type,
             trigger_description, when_raw, now, next_run),
        )
        conn.commit()
    return get_reminder(reminder_id)


def update_reminder(reminder_id: str, **kwargs) -> dict[str, Any] | None:
    allowed = {"message", "label", "recipients", "trigger_type", "trigger_description",
               "when_raw", "status", "next_run", "last_run"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return get_reminder(reminder_id)
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [reminder_id]
    with _connect() as conn:
        conn.execute(f"UPDATE reminders SET {sets} WHERE id = ?", values)
        conn.commit()
    return get_reminder(reminder_id)


def mark_fired(reminder_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE reminders SET last_run = ? WHERE id = ?",
            (now, reminder_id),
        )
        conn.commit()


def mark_completed(reminder_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'completed', last_run = ? WHERE id = ?",
            (now, reminder_id),
        )
        conn.commit()


def mark_cancelled(reminder_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ?",
            (reminder_id,),
        )
        conn.commit()


# ── Read ───────────────────────────────────────────────────────────────────────

def get_reminder(reminder_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_reminders(chat_id: str | None = None, status: str = "active") -> list[dict[str, Any]]:
    with _connect() as conn:
        if chat_id:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE chat_id = ? AND status = ? ORDER BY created_at DESC",
                (chat_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_active() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'active' ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def purge_old(days: int = 30) -> int:
    """Delete completed/cancelled reminders older than `days` days. Returns rows deleted."""
    cutoff_iso = f"-{int(days)} days"
    with _connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM reminders
             WHERE status IN ('completed', 'cancelled')
               AND COALESCE(last_run, created_at) < datetime('now', ?)
            """,
            (cutoff_iso,),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted
