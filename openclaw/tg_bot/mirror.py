"""Cross-surface mirror — forwards brain events to Telegram.

When a user sends a message via the Mini App (or any non-Telegram surface)
and that conversation has a Telegram binding for the same thread, this
module forwards both the user message and the assistant reply to the
bound TG chat.

Result: send from Mini App → message + reply also lands on Telegram.
Bidirectional parity with the TG-first flow (which already pushes to the
Mini App via the existing `/ws/brain` WebSocket).

The module exposes two functions:
  - `decide_outboxes(event, lookup_fns) -> list[Outbox]` — PURE dispatch
    logic, side-effect free, easy to unit-test.
  - `start_mirror(token, store, dry_run=False) -> threading.Thread` —
    spawns a daemon thread that runs `listen_events`, calls
    `decide_outboxes`, and delivers via `sendMessage`.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from openclaw.brain_store import BrainStore
from openclaw.eventbus import listen_events
from openclaw.tg_bot.dispatcher import Outbox, _escape


logger = logging.getLogger("openclaw.tg_bot.mirror")


# Telegram body-length cap; messages above this get split into chunks.
_TG_MAX_BODY = 3800


@dataclass
class MessageRecord:
    """Minimal projection of brain.messages we need to render."""
    id: int
    role: str
    content: str
    surface: str | None


@dataclass
class _LookupFns:
    """Three small queries the mirror needs against brain_store.
    Pulled into a struct so `decide_outboxes` can be unit-tested with
    stubs and never touches Postgres."""
    fetch_message: Callable[[int], MessageRecord | None]
    trigger_surface_of: Callable[[int, str], str | None]   # (assistant_id, thread_id) → user.surface
    tg_chat_for_thread: Callable[[str], tuple[int, int | None] | None]   # → (chat_id, topic_id?) or None


def _split_for_telegram(text: str) -> list[str]:
    """TG sendMessage caps at 4096 chars; keep some slack for HTML tags."""
    out: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _TG_MAX_BODY:
            out.append(remaining)
            break
        # Try to split on a paragraph boundary near the cap
        cut = remaining.rfind("\n\n", 0, _TG_MAX_BODY)
        if cut < _TG_MAX_BODY // 2:
            cut = remaining.rfind("\n", 0, _TG_MAX_BODY)
        if cut < _TG_MAX_BODY // 2:
            cut = _TG_MAX_BODY
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return out


def decide_outboxes(event: dict, lookups: _LookupFns) -> list[Outbox]:
    """Pure dispatch — given a brain_events payload + lookup functions,
    return the list of Telegram messages to send. No side effects."""
    kind = event.get("kind")
    if kind not in ("message.new", "message.complete"):
        return []
    thread_id = event.get("thread_id")
    if not thread_id:
        return []

    chat_topic = lookups.tg_chat_for_thread(thread_id)
    if chat_topic is None:
        # No Telegram binding for this thread — silently skip.
        return []
    chat_id, topic_id = chat_topic

    role = event.get("role")
    msg_id = event.get("message_id")
    if not msg_id:
        return []

    if kind == "message.new":
        # User messages from non-TG surfaces are mirrored via Telethon in the
        # bridge process (see sentinel-miniapp-v2/tg_user_mirror.py) so they
        # appear as the owner's TG account, not the bot. The sidecar's job
        # here is only assistant replies, below.
        return []

    # message.complete — forward assistant replies whose trigger user was non-TG
    if role != "assistant":
        return []
    trigger_surface = (lookups.trigger_surface_of(msg_id, thread_id) or "").lower()
    if trigger_surface in ("telegram", ""):
        # Either the TG bot already sent this via the normal flow, or we
        # can't determine the trigger (be conservative).
        return []
    rec = lookups.fetch_message(msg_id)
    if rec is None or not (rec.content or "").strip():
        return []
    # Telegram's HTML subset is narrower than what the assistant emits
    # (markdown). Escape everything; OpenClaw's `_via tool_` markers
    # stay readable as plain text.
    body = _escape(rec.content)
    return [Outbox(chat_id, chunk, parse_mode="HTML", message_thread_id=topic_id) for chunk in _split_for_telegram(body)]


# ── DB-backed lookups (the live wiring) ──────────────────────────────


def _make_live_lookups(store: BrainStore) -> _LookupFns:
    import psycopg
    from psycopg.rows import dict_row

    def fetch_message(mid: int) -> MessageRecord | None:
        with psycopg.connect(store.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, role, content, surface FROM brain.messages WHERE id = %s",
                (mid,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return MessageRecord(
            id=row["id"], role=row["role"],
            content=row["content"] or "", surface=row.get("surface"),
        )

    def trigger_surface_of(assistant_id: int, thread_id: str) -> str | None:
        import uuid as _u
        tid = _u.UUID(str(thread_id))
        with psycopg.connect(store.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT surface FROM brain.messages
                 WHERE conv_id = %s AND role = 'user' AND id < %s
                 ORDER BY id DESC LIMIT 1
                """,
                (tid, assistant_id),
            )
            row = cur.fetchone()
        return row["surface"] if row else None

    def tg_chat_for_thread(thread_id: str) -> tuple[int, int | None] | None:
        import uuid as _u
        tid = _u.UUID(str(thread_id))
        with psycopg.connect(store.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT surface_account, tg_topic_id FROM brain.surface_bindings
                 WHERE surface = 'telegram' AND active_thread_id = %s
                 ORDER BY (tg_topic_id IS NOT NULL) DESC, updated_at DESC
                 LIMIT 1
                """,
                (tid,),
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            return (int(row["surface_account"]), row["tg_topic_id"])
        except (TypeError, ValueError):
            return None

    return _LookupFns(
        fetch_message=fetch_message,
        trigger_surface_of=trigger_surface_of,
        tg_chat_for_thread=tg_chat_for_thread,
    )


def start_mirror(
    token: str,
    store: BrainStore,
    deliver_fn: Callable[[str, list[Outbox], bool], None],
    dry_run: bool = False,
) -> threading.Thread:
    """Spawn the daemon thread that subscribes to brain_events and forwards
    non-TG-origin events to Telegram.  Returns the thread (already started).
    """
    lookups = _make_live_lookups(store)

    def _run():
        logger.info("cross-surface mirror starting (dry_run=%s)", dry_run)
        for event in listen_events(store.dsn, poll_timeout=1.0):
            try:
                outboxes = decide_outboxes(event, lookups)
            except Exception as exc:
                logger.exception("decide_outboxes raised on event %s", event.get("kind"))
                outboxes = []
            if outboxes:
                try:
                    deliver_fn(token, outboxes, dry_run)
                except Exception as exc:
                    logger.warning("mirror deliver failed: %s", exc)

    t = threading.Thread(target=_run, daemon=True, name="cross-surface-mirror")
    t.start()
    return t
