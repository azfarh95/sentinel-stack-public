"""Cross-process brain event bus on Postgres LISTEN/NOTIFY.

Producers (brain_store inserts, tg_bot, brain_wrapper) call `emit_event`
inside their transaction.  The Postgres NOTIFY fires on COMMIT — so a
rolled-back INSERT also drops its notification, no extra coordination
needed.

Consumers (Mini App's WebSocket fan-out, Tauri admin's WS subscriber,
anything else interested in live brain state) call `listen_events` in a
background thread; it yields decoded events forever, reconnecting on
transient failures.

Channel name: `brain_events`.  Payload: JSON dict with at minimum a
`kind` field.  Postgres NOTIFY payload limit is ~8000 bytes; we keep
payloads small (id refs + minimal metadata) rather than full message
content.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Iterator

import psycopg


CHANNEL = "brain_events"
PAYLOAD_LIMIT = 7500  # leave headroom under PG's ~8000-byte cap
logger = logging.getLogger("openclaw.eventbus")


def emit_event(payload: dict, dsn: str, *, conn: psycopg.Connection | None = None) -> None:
    """Fire a NOTIFY on the brain_events channel.

    If `conn` is provided the NOTIFY rides on that transaction — preferred
    so the event only fires if the caller's INSERT/UPDATE commits.
    Otherwise opens a one-shot connection in autocommit mode (fire-and-
    forget; the row may roll back, in which case subscribers will see a
    stale event referring to a non-existent message).  Use the
    `conn=` form whenever possible.
    """
    body = json.dumps(payload, default=str, ensure_ascii=False)
    if len(body) > PAYLOAD_LIMIT:
        # Strip optional bulky fields; preserve identity so subscribers
        # can still react and refetch.
        slim = dict(payload)
        for k in ("content", "tool_calls", "tool_result"):
            slim.pop(k, None)
        slim["_truncated"] = True
        body = json.dumps(slim, default=str, ensure_ascii=False)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(f"SELECT pg_notify('{CHANNEL}', %s)", (body,))
        return
    # Fire-and-forget path
    try:
        with psycopg.connect(dsn, autocommit=True) as oc, oc.cursor() as cur:
            cur.execute(f"SELECT pg_notify('{CHANNEL}', %s)", (body,))
    except Exception as exc:
        logger.warning("emit_event one-shot failed: %s", exc)


def listen_events(dsn: str, *, poll_timeout: float = 1.0) -> Iterator[dict]:
    """Yield decoded brain events forever.  Reconnects on errors.

    Caller is expected to run this in a background thread and is
    responsible for shutting it down (e.g. by setting a stop flag in
    the consumer-side context and breaking out, or by exiting the
    process).
    """
    backoff = 1.0
    while True:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"LISTEN {CHANNEL}")
                logger.info("eventbus LISTEN %s connected", CHANNEL)
                backoff = 1.0
                # psycopg3: conn.notifies(timeout=...) yields Notify objects
                while True:
                    gen = conn.notifies(timeout=poll_timeout)
                    for notif in gen:
                        try:
                            yield json.loads(notif.payload)
                        except Exception as exc:
                            logger.warning("bad NOTIFY payload: %s (%s)", exc, notif.payload[:200])
        except Exception as exc:
            logger.warning("listen_events disconnected (%s); reconnect in %.1fs", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
