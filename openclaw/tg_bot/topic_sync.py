"""Brain-store thread → Telegram forum topic auto-creation.

When a thread is created from a non-Telegram surface (Mini App, Tauri,
CLI), the brain_store fires a `thread.updated` event with `field=created`.
This listener catches that, calls `createForumTopic` against the owner's
known TG forum chat, and persists the resulting topic_id into
surface_bindings so subsequent TG messages route into that topic.

Also exports a small helper for the `/rename` command path that uses
`editForumTopic` to keep the TG topic name in sync with brain_store.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from typing import Callable

from openclaw.brain_store import BrainStore
from openclaw.eventbus import listen_events


logger = logging.getLogger("openclaw.tg_bot.topic_sync")


SURFACE = "telegram"


def _api_call(token: str, method: str, params: dict, timeout: int = 15) -> dict:
    """One-shot Telegram Bot API POST. Returns the parsed JSON response."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def create_forum_topic(token: str, chat_id: int, name: str,
                       icon_color: int | None = None) -> int | None:
    """Create a forum topic. Returns the new topic's message_thread_id, or
    None on failure. Picks a sensible icon colour if not specified — TG's
    accepted palette is documented at the Bot API page."""
    params: dict[str, str] = {"chat_id": str(chat_id), "name": name[:128]}
    if icon_color is not None:
        params["icon_color"] = str(icon_color)
    try:
        resp = _api_call(token, "createForumTopic", params)
    except Exception as exc:
        logger.warning("createForumTopic failed (chat=%s name=%r): %s", chat_id, name, exc)
        return None
    if not resp.get("ok"):
        logger.warning("createForumTopic rejected: %s", resp)
        return None
    return resp["result"].get("message_thread_id")


def edit_forum_topic(token: str, chat_id: int, topic_id: int, name: str) -> bool:
    """Rename an existing forum topic. Bot must have manage-topics permission."""
    params = {
        "chat_id": str(chat_id),
        "message_thread_id": str(topic_id),
        "name": name[:128],
    }
    try:
        resp = _api_call(token, "editForumTopic", params)
    except Exception as exc:
        logger.warning("editForumTopic failed (chat=%s topic=%s name=%r): %s",
                       chat_id, topic_id, name, exc)
        return False
    if not resp.get("ok"):
        logger.warning("editForumTopic rejected: %s", resp)
        return False
    return True


def close_forum_topic(token: str, chat_id: int, topic_id: int) -> bool:
    """Close (archive) a forum topic. Bot needs manage_topics permission."""
    try:
        resp = _api_call(token, "closeForumTopic", {
            "chat_id": str(chat_id), "message_thread_id": str(topic_id),
        })
    except Exception as exc:
        logger.warning("closeForumTopic failed (chat=%s topic=%s): %s",
                       chat_id, topic_id, exc)
        return False
    if not resp.get("ok"):
        logger.warning("closeForumTopic rejected: %s", resp)
        return False
    return True


def reopen_forum_topic(token: str, chat_id: int, topic_id: int) -> bool:
    """Reopen a previously-closed forum topic."""
    try:
        resp = _api_call(token, "reopenForumTopic", {
            "chat_id": str(chat_id), "message_thread_id": str(topic_id),
        })
    except Exception as exc:
        logger.warning("reopenForumTopic failed (chat=%s topic=%s): %s",
                       chat_id, topic_id, exc)
        return False
    return bool(resp.get("ok"))


def _primary_forum_chat(store: BrainStore) -> int | None:
    """Discover the owner's 'primary forum chat' = the most-recently-used
    Telegram surface_binding that has a topic_id set. If none exists yet
    (first-run, owner hasn't touched a forum topic), returns None and we
    skip topic creation; the thread still exists, just no TG topic yet."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(store.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT surface_account FROM brain.surface_bindings
             WHERE surface = 'telegram' AND tg_topic_id IS NOT NULL
             ORDER BY updated_at DESC LIMIT 1
            """,
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        return int(row["surface_account"])
    except (TypeError, ValueError):
        return None


def _thread_already_has_topic(store: BrainStore, thread_id: str) -> bool:
    """True if this thread is already bound to at least one TG topic."""
    import uuid as _u
    import psycopg
    with psycopg.connect(store.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM brain.surface_bindings
             WHERE surface = 'telegram'
               AND active_thread_id = %s
               AND tg_topic_id IS NOT NULL
             LIMIT 1
            """,
            (_u.UUID(str(thread_id)),),
        )
        return cur.fetchone() is not None


def start_topic_sync(token: str, store: BrainStore,
                     owner_user: str = "azfar",
                     forum_chat_id: int | None = None) -> threading.Thread:
    """Spawn the daemon thread that listens for brain `thread.updated{field:created}`
    events and creates matching TG forum topics.

    `forum_chat_id` pins the supergroup topics are created in. When given it
    overrides the older "most-recently-used binding" discovery — explicit
    config is more robust than inferring the chat from binding history."""

    def _run():
        logger.info("topic_sync listener starting (forum_chat_id=%s)", forum_chat_id)
        for event in listen_events(store.dsn, poll_timeout=1.0):
            try:
                _handle_event(event, token, store, owner_user, forum_chat_id)
            except Exception:
                logger.exception("topic_sync tick crashed on event %s", event.get("kind"))

    t = threading.Thread(target=_run, daemon=True, name="tg-topic-sync")
    t.start()
    return t


def _bindings_for_thread(store: BrainStore, thread_id: str) -> list[tuple[int, int]]:
    """All (chat_id, topic_id) pairs that route to this thread. Used for
    archive: when the thread closes, we close every bound topic."""
    import uuid as _u
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(store.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT surface_account, tg_topic_id FROM brain.surface_bindings
             WHERE surface = 'telegram'
               AND active_thread_id = %s
               AND tg_topic_id IS NOT NULL
            """,
            (_u.UUID(str(thread_id)),),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        try:
            out.append((int(r["surface_account"]), int(r["tg_topic_id"])))
        except (TypeError, ValueError):
            continue
    return out


def _handle_event(event: dict, token: str, store: BrainStore, owner_user: str,
                  forum_chat_id: int | None = None) -> None:
    if event.get("kind") != "thread.updated":
        return
    if event.get("user_id") and event["user_id"] != owner_user:
        return
    thread_id = event.get("thread_id")
    if not thread_id:
        return
    field = event.get("field")

    if field == "created":
        _handle_created(event, token, store, owner_user, thread_id, forum_chat_id)
    elif field == "archived_at":
        _handle_archived(event, token, store, thread_id)
    elif field == "name":
        _handle_renamed(event, token, store, thread_id)


def _handle_created(event: dict, token: str, store: BrainStore,
                    owner_user: str, thread_id: str,
                    forum_chat_id: int | None = None) -> None:
    name = event.get("name") or ""
    if not name:
        return
    # Skip thread names the dispatcher auto-generates for already-existing
    # topics — those came FROM TG already and have their own bindings.
    if name.startswith("topic-"):
        return
    # Skip if this thread is already bound to a TG topic (idempotent).
    if _thread_already_has_topic(store, thread_id):
        return
    # Prefer the explicitly configured forum chat; fall back to discovery.
    chat_id = forum_chat_id if forum_chat_id is not None else _primary_forum_chat(store)
    if chat_id is None:
        logger.info(
            "topic_sync: no forum chat configured or discovered yet — thread %s "
            "stays without a TG topic until SENTINEL_TG_FORUM_CHAT_ID is set "
            "or the owner uses a TG forum topic at least once",
            thread_id,
        )
        return
    topic_id = create_forum_topic(token, chat_id, name)
    if topic_id is None:
        return
    store.set_active_thread(
        surface=SURFACE,
        surface_account=str(chat_id),
        thread_id=thread_id,
        user_id=owner_user,
        tg_topic_id=topic_id,
    )
    logger.info(
        "topic_sync: created TG topic chat=%s topic=%s for thread=%s name=%r",
        chat_id, topic_id, thread_id, name,
    )


def _handle_renamed(event: dict, token: str, store: BrainStore,
                    thread_id: str) -> None:
    """A brain thread was renamed → push the new title to every TG topic
    bound to it. Idempotent: if the topic title already matches (e.g. the
    rename originated in TG), editForumTopic is a harmless no-op and the
    dispatcher's _on_topic_renamed guard stops any echo loop."""
    name = event.get("name") or ""
    if not name:
        return
    for chat_id, topic_id in _bindings_for_thread(store, thread_id):
        ok = edit_forum_topic(token, chat_id, topic_id, name)
        logger.info(
            "topic_sync: rename sync chat=%s topic=%s thread=%s name=%r ok=%s",
            chat_id, topic_id, thread_id, name, ok,
        )


def _handle_archived(event: dict, token: str, store: BrainStore,
                     thread_id: str) -> None:
    """When a thread is archived in brain_store, close every TG topic
    bound to it. Idempotent — calling closeForumTopic on an already-closed
    topic is a no-op (returns ok=False, we log+ignore)."""
    bindings = _bindings_for_thread(store, thread_id)
    if not bindings:
        return
    for chat_id, topic_id in bindings:
        ok = close_forum_topic(token, chat_id, topic_id)
        logger.info(
            "topic_sync: archive sync chat=%s topic=%s thread=%s closed=%s",
            chat_id, topic_id, thread_id, ok,
        )
