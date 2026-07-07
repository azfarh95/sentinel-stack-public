"""Cross-surface user-message mirror — sends via Telethon AS THE USER.

When you type a message in /chat (Mini App), this listener forwards it
to your Telegram bot DM as if YOU typed it there. Uses Telethon's
user-account API so the message shows up under your name + avatar, not
the bot.

Sibling to `openclaw/tg_bot/mirror.py` which handles the OTHER direction
of the mirror (assistant replies → Bot API → bot sends).

Split rationale:
- Bridge owns Telethon (already wired for the /api/agent/message composer)
- Sidecar owns the Bot API token
- One listener per process, each handles its concern

Both listen to the same `brain_events` Postgres channel; filters keep
them from stepping on each other.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openclaw.brain_store import BrainStore  # noqa: E402
from openclaw.eventbus import listen_events  # noqa: E402


logger = logging.getLogger("sentinel.miniapp.tg_user_mirror")


def _should_forward(event: dict) -> bool:
    """True iff this event is a finalised user message from a non-TG surface."""
    if event.get("kind") != "message.new":
        return False
    if event.get("role") != "user":
        return False
    if event.get("streaming_done") is False:
        return False
    surface = (event.get("surface") or "").lower()
    return surface not in ("telegram", "", "server")


def _tg_chat_for_thread(store: BrainStore, thread_id: str) -> tuple[int, int | None] | None:
    """Return (chat_id, topic_id) for the most-recently-active TG binding of
    this thread. None if there's no TG binding at all."""
    import uuid as _u
    import psycopg
    from psycopg.rows import dict_row
    try:
        tid = _u.UUID(str(thread_id))
    except Exception:
        return None
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


def _fetch_content(store: BrainStore, message_id: int) -> str | None:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(store.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT content FROM brain.messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
    return (row["content"] if row else None) or None


def start_user_mirror(
    *,
    get_telethon: Callable[[], tuple[object, object]] | None = None,
    bot_chat_id: int | None = None,
) -> threading.Thread:
    """Spawn the daemon thread that forwards non-TG user messages to TG via
    Telethon. Returns the started thread.

    Args:
        get_telethon: callable returning (client, loop) so we can pick up
                      the bridge's already-running Telethon. Lazy so we
                      don't crash if Telethon isn't ready at import time.
        bot_chat_id: the TG bot's chat id. We send TO that chat so the
                     message lands in your DM with the bot. If None, the
                     mirror sends to whatever chat is bound to the thread
                     via surface_bindings (typically the same).
    """
    store = BrainStore()

    def _send_as_user(target_chat_id: int, text: str, topic_id: int | None = None) -> bool:
        if get_telethon is None:
            logger.warning("no telethon accessor — drop forward to chat=%s", target_chat_id)
            return False
        client, loop = get_telethon()
        if client is None or loop is None:
            logger.warning("telethon not ready — drop forward to chat=%s", target_chat_id)
            return False

        async def _coro():
            # Telethon accepts `reply_to=<topic_id>` to route a message into
            # a specific forum topic (it doubles as both reply-target and
            # topic indicator per the MTProto schema).
            kwargs = {"reply_to": topic_id} if topic_id is not None else {}
            return await client.send_message(target_chat_id, text, **kwargs)

        try:
            fut = asyncio.run_coroutine_threadsafe(_coro(), loop)
            fut.result(timeout=10)
            return True
        except Exception as exc:
            logger.warning("telethon send failed (chat=%s topic=%s): %s",
                           target_chat_id, topic_id, exc)
            return False

    def _run():
        logger.info("tg_user_mirror starting")
        for event in listen_events(store.dsn, poll_timeout=1.0):
            try:
                if not _should_forward(event):
                    continue
                thread_id = event.get("thread_id")
                message_id = event.get("message_id")
                if not thread_id or not message_id:
                    continue
                # Prefer the thread's bound TG (chat, topic) when known; else
                # fall back to the bot_chat_id with no topic.
                bound = _tg_chat_for_thread(store, thread_id)
                if bound is not None:
                    chat_id, topic_id = bound
                else:
                    chat_id, topic_id = (bot_chat_id, None) if bot_chat_id else (None, None)
                if chat_id is None:
                    continue
                content = _fetch_content(store, message_id)
                if not content or not content.strip():
                    continue
                if _send_as_user(chat_id, content, topic_id=topic_id):
                    logger.info("forwarded msg id=%s to chat=%s topic=%s via telethon",
                                message_id, chat_id, topic_id)
            except Exception:
                logger.exception("tg_user_mirror tick crashed")

    t = threading.Thread(target=_run, daemon=True, name="tg-user-mirror")
    t.start()
    return t
