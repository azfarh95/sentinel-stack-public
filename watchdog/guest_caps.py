"""
guest_caps.py — per-tester daily message cap for shared OpenRouter beta.

Source of truth: ~/.openclaw/agents/main/sessions/sessions.json — each entry
has a sessionKey like "agent:main:telegram:direct:<chat_id>" and a
`lastInteractionAt` timestamp. We poll, detect when that timestamp advances
for non-owner chat_ids, and increment a daily counter.

When a guest hits their cap:
  1. They're removed from telegram-default-allowFrom.json (OpenClaw stops
     responding to them)
  2. Owner gets a Telegram alert
At midnight local time, counters reset and throttled users are re-added.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# WSL UNC paths — readable from the watchdog (Windows side)
SESSIONS_JSON = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\sessions.json"
ALLOW_FROM_JSON = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\credentials\telegram-default-allowFrom.json"

DB_PATH = Path(__file__).parent / "guest_usage.db"

# Tighter than the 1000/day OpenRouter free quota across ~10 testers
DEFAULT_DAILY_MESSAGE_CAP = 50

_lock = threading.Lock()


# ── DB ────────────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS usage (
                chat_id    TEXT NOT NULL,
                day_local  TEXT NOT NULL,
                messages   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, day_local)
            );
            CREATE TABLE IF NOT EXISTS caps (
                chat_id        TEXT PRIMARY KEY,
                max_messages   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                chat_id           TEXT PRIMARY KEY,
                last_interaction  INTEGER NOT NULL DEFAULT 0,
                throttled         INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.commit()


def _today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── Cap config ────────────────────────────────────────────────────────────────

def get_cap(chat_id: str, default: int = DEFAULT_DAILY_MESSAGE_CAP) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT max_messages FROM caps WHERE chat_id = ?", (chat_id,)).fetchone()
    return int(row["max_messages"]) if row else default


def set_cap(chat_id: str, max_messages: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO caps (chat_id, max_messages) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET max_messages = excluded.max_messages",
            (chat_id, max_messages),
        )
        conn.commit()


# ── Usage counters ────────────────────────────────────────────────────────────

def get_usage(chat_id: str, day: str | None = None) -> int:
    day = day or _today_local()
    with _connect() as conn:
        row = conn.execute(
            "SELECT messages FROM usage WHERE chat_id = ? AND day_local = ?",
            (chat_id, day),
        ).fetchone()
    return int(row["messages"]) if row else 0


def _increment(chat_id: str, day: str, n: int = 1) -> int:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO usage (chat_id, day_local, messages) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, day_local) DO UPDATE SET messages = messages + ?",
            (chat_id, day, n, n),
        )
        row = conn.execute(
            "SELECT messages FROM usage WHERE chat_id = ? AND day_local = ?",
            (chat_id, day),
        ).fetchone()
        conn.commit()
    return int(row["messages"]) if row else n


def list_usage(day: str | None = None) -> list[dict]:
    """All non-zero usage rows for a day, joined with caps/throttle state."""
    day = day or _today_local()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT u.chat_id, u.messages, c.max_messages, s.throttled
              FROM usage u
              LEFT JOIN caps  c USING (chat_id)
              LEFT JOIN state s USING (chat_id)
             WHERE u.day_local = ?
             ORDER BY u.messages DESC
            """,
            (day,),
        ).fetchall()
    return [{
        "chat_id":      r["chat_id"],
        "messages":     int(r["messages"] or 0),
        "max_messages": int(r["max_messages"] or DEFAULT_DAILY_MESSAGE_CAP),
        "throttled":    bool(r["throttled"]),
    } for r in rows]


# ── allowFrom toggle (the enforcement primitive) ──────────────────────────────

def _read_allow_from() -> dict:
    try:
        with open(ALLOW_FROM_JSON, encoding="utf-8") as f:
            return json.load(f) or {"version": 1, "allowFrom": []}
    except Exception:
        return {"version": 1, "allowFrom": []}


def _write_allow_from(data: dict) -> None:
    with open(ALLOW_FROM_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _set_throttled(chat_id: str, throttled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO state (chat_id, throttled) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET throttled = excluded.throttled",
            (chat_id, 1 if throttled else 0),
        )
        conn.commit()


def throttle(chat_id: str) -> bool:
    """Remove chat_id from allowFrom and mark in state. Returns True if changed."""
    with _lock:
        af = _read_allow_from()
        before = list(af.get("allowFrom", []))
        if chat_id not in {str(x) for x in before}:
            _set_throttled(chat_id, True)
            return False  # already not in list, but record state
        af["allowFrom"] = [x for x in before if str(x) != chat_id]
        _write_allow_from(af)
    _set_throttled(chat_id, True)
    return True


def unthrottle(chat_id: str) -> bool:
    """Re-add chat_id to allowFrom. Returns True if changed."""
    with _lock:
        af = _read_allow_from()
        existing = {str(x) for x in af.get("allowFrom", [])}
        if chat_id in existing:
            _set_throttled(chat_id, False)
            return False
        af.setdefault("allowFrom", []).append(chat_id)
        _write_allow_from(af)
    _set_throttled(chat_id, False)
    return True


def list_throttled() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT chat_id FROM state WHERE throttled = 1").fetchall()
    return [r["chat_id"] for r in rows]


# ── Polling tick ──────────────────────────────────────────────────────────────

def _read_sessions() -> list[tuple[str, int]]:
    """Return list of (chat_id, lastInteractionAt_ms) for telegram direct sessions."""
    try:
        with open(SESSIONS_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for key, info in (data or {}).items():
        # key looks like "agent:main:telegram:direct:YOUR_TELEGRAM_CHAT_ID"
        if not isinstance(info, dict):
            continue
        if ":telegram:" not in key:
            continue
        chat_id = key.rsplit(":", 1)[-1]
        last = int(info.get("lastInteractionAt", 0) or 0)
        out.append((chat_id, last))
    return out


def tick(owner_chat_id: str) -> dict:
    """Run one polling cycle. Detects new interactions, increments usage,
    applies/removes throttle. Returns a summary dict with any cap events."""
    today = _today_local()
    sessions = _read_sessions()
    new_throttles: list[str] = []
    summary = {"polled": len(sessions), "incremented": 0, "throttled": new_throttles}

    for chat_id, last in sessions:
        if chat_id == str(owner_chat_id):
            continue  # owner is unlimited
        # Read previous last-interaction
        with _connect() as conn:
            row = conn.execute("SELECT last_interaction FROM state WHERE chat_id = ?", (chat_id,)).fetchone()
            prev = int(row["last_interaction"]) if row else 0
            if last <= prev:
                continue  # no new activity since last tick
            conn.execute(
                "INSERT INTO state (chat_id, last_interaction) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET last_interaction = excluded.last_interaction",
                (chat_id, last),
            )
            conn.commit()
        # New interaction → +1 message today (approximate; the polling cycle
        # may collapse rapid-fire messages but for a 50/day cap that's fine)
        new_count = _increment(chat_id, today, 1)
        summary["incremented"] += 1
        cap = get_cap(chat_id)
        if new_count >= cap:
            if throttle(chat_id):
                new_throttles.append(chat_id)
    return summary


def reset_day_if_rolled() -> list[str]:
    """If today differs from the last-rolled day stored in state, reset all
    throttled flags + restore them to allowFrom. Returns list of restored chat_ids."""
    restored: list[str] = []
    today = _today_local()
    with _connect() as conn:
        meta_row = conn.execute("SELECT day_local FROM usage ORDER BY day_local DESC LIMIT 1").fetchone()
    last_day = meta_row["day_local"] if meta_row else None
    if last_day == today:
        return restored
    # New day — restore everyone we throttled
    for cid in list_throttled():
        if unthrottle(cid):
            restored.append(cid)
    return restored


# ── Background runner ─────────────────────────────────────────────────────────

class GuestCapMonitor:
    """Polls sessions.json, applies caps. Sends owner alerts via the bot."""

    def __init__(self, bot, interval: int = 60):
        self.bot      = bot
        self.interval = interval
        self._thread  = threading.Thread(target=self._run, daemon=True, name="guest-caps")
        self._stop    = threading.Event()

    def start(self) -> None:
        init_db()
        self._thread.start()
        print(f"[guest-caps] started (interval={self.interval}s, default cap={DEFAULT_DAILY_MESSAGE_CAP})")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                restored = reset_day_if_rolled()
                if restored:
                    self.bot.send(self.bot.owner,
                        f"🔓 New day — restored guests to allowFrom: <code>{', '.join(restored)}</code>")
                summary = tick(owner_chat_id=str(self.bot.owner))
                for cid in summary.get("throttled", []):
                    cap = get_cap(cid)
                    self.bot.send(self.bot.owner,
                        f"⚠️ Guest <code>{cid}</code> hit daily cap "
                        f"({cap} messages). Removed from allowFrom until midnight local.")
            except Exception as e:
                print(f"[guest-caps] tick error: {e}")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
