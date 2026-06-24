"""Sentinel Shared Brain — persistence module (Phase 1).

Lives in metamcp-pg under the `brain` schema (see migrations/0001_brain_store.sql).
A single conversation store shared by every Sentinel client surface
(Telegram bot, Mini App, TWA, Tauri admin). See docs/shared-brain-plan.md
for the full design.

Single-user owner workload — uses per-call `psycopg.connect()` rather than
a long-lived pool. Phase 4/5 may wrap this in a FastAPI app context.

Token estimation: chars/4 default, matching the existing
`bridge.get_context_estimate()` heuristic. Wire a real tokenizer in
Phase 2 via the `token_counter` constructor arg.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import psycopg
from psycopg.rows import dict_row

from openclaw.eventbus import emit_event


logger = logging.getLogger("openclaw.brain_store")

DEFAULT_USER = "azfar"


# ── env self-load (A-resolution 3.4 / P5 — eventbus PG-auth drift) ───────
# Every consumer that imports brain_store (the bot, its mirror/topic_sync/
# scheduler daemon threads, and the web bridge) builds its Postgres DSN from
# os.environ. When a process is restarted via the env-less SentinelX task
# wrapper, .env.local is NEVER sourced, so POSTGRES_PASSWORD is empty and every
# eventbus reconnect logs `password authentication failed for metamcp_user`
# (was ~24×/day). Self-load the few DSN-relevant keys here, at import, so the
# fix covers ALL consumers in one place — generalising the per-process fix that
# already lives in the web bridge. NO-OVERRIDE: an explicit launcher export
# wins; we only fill gaps. We deliberately skip POSTGRES_PORT/HOST — those are
# the in-container values (postgres:5432); a host process must use the loopback
# POSTGRES_EXTERNAL_PORT (default 9433) instead, so importing them would break
# the connection rather than fix it.
_ENV_LOCAL_PATH = os.environ.get(
    "ENV_LOCAL_PATH", r"C:\Users\azfar\metamcp-local\.env.local"
)
_ENV_SELF_LOAD_KEYS = (
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_EXTERNAL_PORT",
)


def _load_env_local(
    path: str = _ENV_LOCAL_PATH, keys: Iterable[str] = _ENV_SELF_LOAD_KEYS
) -> None:
    """Best-effort fill of the DSN-relevant env vars from .env.local IF MISSING.

    No-override (a launcher's export is authoritative) and never raises — a
    missing file or a parse hiccup must not break import for a process whose
    env was already set correctly. Returns nothing; mutates os.environ."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return
    wanted = set(keys)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k in wanted and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


# Run once at import — before any BrainStore instance builds its DSN.
_load_env_local()


def _default_dsn() -> str:
    """Build the brain-store DSN from env — NO secret hardcoded in source.
    The bot is a host process, so it reaches Postgres on the loopback-published
    port (127.0.0.1:POSTGRES_EXTERNAL_PORT), not the in-container hostname. The
    DSN-relevant POSTGRES_* keys are self-loaded from .env.local at import (see
    `_load_env_local`) so an env-less restart still authenticates; an explicit
    launcher export still wins. Falls back to a passwordless DSN if
    POSTGRES_PASSWORD is absent (connection then fails loudly rather than
    silently using a stale baked-in password)."""
    user = os.environ.get("POSTGRES_USER", "metamcp_user")
    pw = os.environ.get("POSTGRES_PASSWORD", "")
    port = os.environ.get("POSTGRES_EXTERNAL_PORT", "9433")
    db = os.environ.get("POSTGRES_DB", "metamcp_db")
    return f"postgresql://{user}:{pw}@127.0.0.1:{port}/{db}"


_DEFAULT_SUMMARISER = None


def _get_default_summariser():
    """Lazy import — keeps brain_store importable without LM Studio reachable."""
    global _DEFAULT_SUMMARISER
    if _DEFAULT_SUMMARISER is None:
        from openclaw.summariser import summarise_via_lm_studio
        _DEFAULT_SUMMARISER = summarise_via_lm_studio
    return _DEFAULT_SUMMARISER


def _default_token_count(text: str) -> int:
    """Heuristic token estimator: 1 token ≈ 4 chars.
    Matches `sentinel-miniapp-v2/bridge.py:get_context_estimate`.
    Phase 2 swaps this for a real tokenizer."""
    return max(1, len(text) // 4)


@dataclass
class Thread:
    id: uuid.UUID
    user_id: str
    name: str
    kind: str
    pinned_context: str | None
    started_at: datetime
    last_active_at: datetime
    archived_at: datetime | None

    @classmethod
    def from_row(cls, row: dict) -> "Thread":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            kind=row["kind"],
            pinned_context=row.get("pinned_context"),
            started_at=row["started_at"],
            last_active_at=row["last_active_at"],
            archived_at=row.get("archived_at"),
        )

    def to_dict(self) -> dict:
        d = {
            "id": str(self.id),
            "user_id": self.user_id,
            "name": self.name,
            "kind": self.kind,
            "pinned_context": self.pinned_context,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_active_at": self.last_active_at.isoformat() if self.last_active_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
        }
        return d


@dataclass
class Message:
    id: int
    conv_id: uuid.UUID
    role: str
    content: str
    surface: str | None
    surface_msg_id: str | None
    tool_calls: Any | None
    tool_result: Any | None
    parent_msg_id: int | None
    tokens_in: int | None
    tokens_out: int | None
    model: str | None
    is_summary: bool
    pinned: bool
    created_at: datetime
    streaming_done: bool

    @classmethod
    def from_row(cls, row: dict) -> "Message":
        return cls(
            id=row["id"],
            conv_id=row["conv_id"],
            role=row["role"],
            content=row["content"],
            surface=row.get("surface"),
            surface_msg_id=row.get("surface_msg_id"),
            tool_calls=row.get("tool_calls"),
            tool_result=row.get("tool_result"),
            parent_msg_id=row.get("parent_msg_id"),
            tokens_in=row.get("tokens_in"),
            tokens_out=row.get("tokens_out"),
            model=row.get("model"),
            is_summary=row.get("is_summary", False),
            pinned=row.get("pinned", False),
            created_at=row["created_at"],
            streaming_done=row.get("streaming_done", True),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conv_id": str(self.conv_id),
            "role": self.role,
            "content": self.content,
            "surface": self.surface,
            "surface_msg_id": self.surface_msg_id,
            "tool_calls": self.tool_calls,
            "tool_result": self.tool_result,
            "parent_msg_id": self.parent_msg_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model": self.model,
            "is_summary": self.is_summary,
            "pinned": self.pinned,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "streaming_done": self.streaming_done,
        }


_VALID_ROLES = {"user", "assistant", "tool", "system"}


class BrainStore:
    """Synchronous persistence layer. Each public method opens + closes its
    own connection — fine at single-user scale; Phase 4/5 will wrap this
    in an async layer with a pool if needed."""

    def __init__(
        self,
        dsn: str | None = None,
        token_counter: Callable[[str], int] | None = None,
        summariser: Callable[[list[dict]], str] | None = None,
        summary_threshold: int = 10,
    ) -> None:
        self.dsn = dsn or os.environ.get("BRAIN_STORE_DSN") or _default_dsn()
        self.token_counter = token_counter or _default_token_count
        # Lazy-import to avoid pulling urllib + LM at import time for tests
        # that never touch summarisation.
        self._summariser = summariser
        self.summary_threshold = summary_threshold

    # ── connection helper ────────────────────────────────────────────
    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    # ── thread ops ───────────────────────────────────────────────────
    def create_thread(
        self,
        user_id: str = DEFAULT_USER,
        name: str = "default",
        kind: str = "general",
        pinned_context: str | None = None,
        thread_id: uuid.UUID | None = None,
    ) -> Thread:
        """Insert a new thread. UNIQUE(user_id, name) — caller catches
        psycopg.errors.UniqueViolation if name collides."""
        tid = thread_id or uuid.uuid4()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain.conversations
                    (id, user_id, name, kind, pinned_context)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (tid, user_id, name, kind, pinned_context),
            )
            row = cur.fetchone()
            emit_event({
                "kind": "thread.updated",
                "thread_id": str(row["id"]),
                "field": "created",
                "user_id": row["user_id"],
                "name": row["name"],
            }, self.dsn, conn=conn)
        return Thread.from_row(row)

    def get_or_create_default(
        self,
        user_id: str = DEFAULT_USER,
        name: str = "default",
        kind: str = "general",
    ) -> Thread:
        """Convenience: idempotent fetch-or-create for the user's default thread."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain.conversations (id, user_id, name, kind)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, name) DO UPDATE
                    SET last_active_at = brain.conversations.last_active_at
                RETURNING *
                """,
                (uuid.uuid4(), user_id, name, kind),
            )
            row = cur.fetchone()
        return Thread.from_row(row)

    def get_thread(self, thread_id: uuid.UUID | str) -> Thread | None:
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM brain.conversations WHERE id = %s", (tid,)
            )
            row = cur.fetchone()
        return Thread.from_row(row) if row else None

    def list_threads(
        self,
        user_id: str = DEFAULT_USER,
        include_archived: bool = False,
    ) -> list[Thread]:
        sql = """
            SELECT * FROM brain.conversations
            WHERE user_id = %s
              {arch_clause}
            ORDER BY last_active_at DESC
        """.format(
            arch_clause="" if include_archived else "AND archived_at IS NULL"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()
        return [Thread.from_row(r) for r in rows]

    def archive(self, thread_id: uuid.UUID | str) -> None:
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain.conversations SET archived_at = now() WHERE id = %s",
                (tid,),
            )
            emit_event({"kind": "thread.updated", "thread_id": str(tid), "field": "archived_at"},
                       self.dsn, conn=conn)

    def rename_thread(self, thread_id: uuid.UUID | str, name: str) -> None:
        """Rename a thread and emit a `thread.updated{field:name}` event so
        every surface (incl. topic_sync → editForumTopic) can react. Use this
        for brain-originated renames (web/API); TG-origin renames already have
        the correct TG title and update the row directly without re-emitting,
        avoiding a rename echo loop."""
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain.conversations SET name = %s WHERE id = %s RETURNING user_id",
                (name, tid),
            )
            row = cur.fetchone()
            if row is None:
                return
            emit_event({
                "kind": "thread.updated",
                "thread_id": str(tid),
                "field": "name",
                "user_id": row["user_id"],
                "name": name,
            }, self.dsn, conn=conn)

    def set_pinned_context(
        self, thread_id: uuid.UUID | str, pinned_context: str | None
    ) -> None:
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain.conversations SET pinned_context = %s WHERE id = %s",
                (pinned_context, tid),
            )

    # ── message ops ──────────────────────────────────────────────────
    def append(
        self,
        conv_id: uuid.UUID | str,
        role: str,
        content: str,
        surface: str | None = None,
        surface_msg_id: str | None = None,
        tool_calls: Any | None = None,
        tool_result: Any | None = None,
        parent_msg_id: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        model: str | None = None,
        is_summary: bool = False,
        pinned: bool = False,
        streaming_done: bool = True,
    ) -> Message:
        if role not in _VALID_ROLES:
            raise ValueError(f"role must be one of {_VALID_ROLES}, got {role!r}")
        tid = uuid.UUID(str(conv_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain.messages (
                    conv_id, role, content, surface, surface_msg_id,
                    tool_calls, tool_result, parent_msg_id,
                    tokens_in, tokens_out, model,
                    is_summary, pinned, streaming_done
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    tid, role, content, surface, surface_msg_id,
                    json.dumps(tool_calls) if tool_calls is not None else None,
                    json.dumps(tool_result) if tool_result is not None else None,
                    parent_msg_id,
                    tokens_in, tokens_out, model,
                    is_summary, pinned, streaming_done,
                ),
            )
            row = cur.fetchone()
            # Touch parent thread so list_threads ordering tracks activity.
            cur.execute(
                "UPDATE brain.conversations SET last_active_at = now() WHERE id = %s",
                (tid,),
            )
            # Atomic NOTIFY on the same transaction so subscribers only see
            # the message if the INSERT commits.  Keep payload small —
            # subscribers refetch full content from the messages table.
            emit_event({
                "kind": "message.new",
                "thread_id": str(tid),
                "message_id": row["id"],
                "role": row["role"],
                "surface": row.get("surface"),
                "streaming_done": row.get("streaming_done", True),
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            }, self.dsn, conn=conn)
        return Message.from_row(row)

    def finalize(
        self,
        message_id: int,
        content: str,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        model: str | None = None,
    ) -> Message:
        """Commit a streaming assistant message: replace placeholder content
        with the final text and flip streaming_done."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brain.messages
                   SET content = %s,
                       tokens_in = COALESCE(%s, tokens_in),
                       tokens_out = COALESCE(%s, tokens_out),
                       model = COALESCE(%s, model),
                       streaming_done = TRUE
                 WHERE id = %s
                 RETURNING *
                """,
                (content, tokens_in, tokens_out, model, message_id),
            )
            row = cur.fetchone()
            if row is not None:
                emit_event({
                    "kind": "message.complete",
                    "thread_id": str(row["conv_id"]),
                    "message_id": row["id"],
                    "role": row["role"],
                    "model": row.get("model"),
                    "tokens_in": row.get("tokens_in"),
                    "tokens_out": row.get("tokens_out"),
                    "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                }, self.dsn, conn=conn)
        if row is None:
            raise KeyError(f"message id {message_id} not found")
        return Message.from_row(row)

    def reap_orphans(self, older_than_minutes: int = 20) -> int:
        """Finalize abandoned in-flight assistant rows as '[interrupted]'.

        A reserved row (`chat_turn_begin` → streaming_done=False) is normally
        finalized by `chat_turn_finish` — including its turn-level fence, which
        catches subprocess timeouts/exceptions. But a hard process death
        (SIGKILL / OOM / host reboot mid-turn) bypasses that fence and leaves
        the row in-flight forever; it then reads as a live "thinking" turn to
        every surface and never frees the (single, `-np 1`) slot in spirit.
        This sweeps rows older than the cutoff — which MUST exceed the longest
        legitimate turn (HARD_TIMEOUT_S = 900 s), so a genuinely in-flight turn
        is never reaped — and finalizes them with the '[interrupted]' sentinel
        the read-side loader filter already excludes. Content is overwritten
        rather than kept: an abandoned turn's partial text must not become
        replayable. Returns the number reaped. (A-resolution 3.3 / P6.)"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brain.messages
                   SET content = '[interrupted]',
                       streaming_done = TRUE
                 WHERE streaming_done = FALSE
                   AND role = 'assistant'
                   AND created_at < now() - make_interval(mins => %s)
                 RETURNING id
                """,
                (older_than_minutes,),
            )
            n = len(cur.fetchall())
        if n:
            logger.info("reap_orphans: finalized %d orphaned in-flight row(s)", n)
        return n

    def pin(self, message_id: int, pinned: bool = True) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain.messages SET pinned = %s WHERE id = %s",
                (pinned, message_id),
            )

    def forget(self, thread_id: uuid.UUID | str, last_n: int) -> int:
        """Soft-redact the most recent N non-summary messages in this thread.
        Preserves the audit trail (rows aren't deleted) but removes the
        content from future `load_for_llm` calls because the redacted
        text counts as 0 tokens and is replaced with `[redacted]`.

        Returns the number of rows redacted."""
        if last_n <= 0:
            return 0
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH victims AS (
                    SELECT id FROM brain.messages
                     WHERE conv_id = %s
                       AND is_summary = FALSE
                     ORDER BY created_at DESC, id DESC
                     LIMIT %s
                )
                UPDATE brain.messages m
                   SET content = '[redacted]',
                       tool_calls = NULL,
                       tool_result = NULL
                  FROM victims v
                 WHERE m.id = v.id
                """,
                (tid, last_n),
            )
            return cur.rowcount

    # ── loader ───────────────────────────────────────────────────────
    def load_for_llm(
        self,
        conv_id: uuid.UUID | str,
        max_tokens: int = 8000,
        *,
        allow_summarise: bool = True,
    ) -> list[dict]:
        """Return the conversation as an OpenAI-shaped message list, walking
        backward from the newest message until the token budget is full.

        Rules:
          * `pinned=TRUE` rows are ALWAYS included (off-budget head).
          * `is_summary=TRUE` rows act as terminal anchors — once a summary
            row is included, the walk-backwards stops (the summary stands in
            for everything older).
          * In-flight rows (`streaming_done=FALSE`) are skipped — they
            haven't finalised yet.
          * **Phase 7 — rolling summarisation**: if the budget would force
            evicting ≥ `summary_threshold` messages and `allow_summarise`
            is True, the loader compresses the to-be-evicted older range
            into a new `is_summary=TRUE` row and re-runs the load.

        Returns dicts shaped like `{"role": str, "content": str,
        "tool_calls"?: ..., "tool_call_id"?: ...}`.
        """
        tid = uuid.UUID(str(conv_id))

        # Pass 1 — count what we'd evict at this budget *without* summarising.
        # If the would-be-evicted count crosses the threshold, summarise that
        # older range first and recurse with allow_summarise=False so we
        # don't loop.
        if allow_summarise:
            evictable = self._count_evictable(tid, max_tokens)
            if evictable >= self.summary_threshold:
                made = self._summarise_oldest(tid, max_tokens)
                if made:
                    # Re-enter with summarisation disabled so a degenerate
                    # case (e.g. summariser produces a huge output) doesn't
                    # recurse.
                    return self.load_for_llm(tid, max_tokens, allow_summarise=False)

        return self._load_within_budget(tid, max_tokens)

    # ── loader helpers ───────────────────────────────────────────────
    def _count_evictable(self, tid: uuid.UUID, max_tokens: int) -> int:
        """How many non-pinned, non-summary, finalised messages would NOT fit
        in the current budget? (i.e. how many oldest messages would be
        dropped if we just truncated.)"""
        budget = max_tokens
        kept = 0
        total = 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, is_summary FROM brain.messages
                 WHERE conv_id = %s
                   AND pinned = FALSE
                   AND streaming_done = TRUE
                   AND NOT (role = 'assistant' AND (
                            content LIKE '[bridge_error]%%'
                         OR content LIKE 'LLM request failed%%'
                         OR content LIKE 'Request timed out before%%'
                         OR content LIKE 'Context overflow: prompt too large%%'
                         OR content LIKE '%%agent returned status=None%%'
                         OR content = '[interrupted]'))
                 ORDER BY created_at DESC, id DESC
                """,
                (tid,),
            )
            for row in cur.fetchall():
                total += 1
                # If we encounter a summary anchor while walking backward,
                # everything older is already represented; nothing to evict.
                if row["is_summary"]:
                    return 0
                cost = self.token_counter(row["content"] or "")
                if cost > budget:
                    continue
                kept += 1
                budget -= cost
        return max(0, total - kept)

    def _summarise_oldest(self, tid: uuid.UUID, max_tokens: int) -> bool:
        """Compress the oldest non-fitting range into one is_summary row.

        Strategy: pick the OLDEST half of messages currently in the thread
        (or all messages older than what fits in budget*0.6, whichever is
        the larger range) and summarise them in one call. Returns True if a
        summary row was created.

        Designed to be a one-shot per load — recursing with
        allow_summarise=False makes the loader stable."""
        # Materialise the chronological message list (oldest → newest).
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, role, content, created_at FROM brain.messages
                 WHERE conv_id = %s
                   AND pinned = FALSE
                   AND streaming_done = TRUE
                   AND is_summary = FALSE
                   AND NOT (role = 'assistant' AND (
                            content LIKE '[bridge_error]%%'
                         OR content LIKE 'LLM request failed%%'
                         OR content LIKE 'Request timed out before%%'
                         OR content LIKE 'Context overflow: prompt too large%%'
                         OR content LIKE '%%agent returned status=None%%'
                         OR content = '[interrupted]'))
                 ORDER BY created_at ASC, id ASC
                """,
                (tid,),
            )
            rows = cur.fetchall()
        if len(rows) < self.summary_threshold:
            return False

        # Walk backward to find which messages would fit in 60% of the
        # budget; everything OLDER than that gets summarised. The 60% gives
        # the loader some headroom for the new summary row itself + the
        # latest few raw turns.
        target_budget = int(max_tokens * 0.6)
        budget = target_budget
        keep_from_idx = len(rows)  # default: keep nothing → summarise all
        for i in range(len(rows) - 1, -1, -1):
            cost = self.token_counter(rows[i]["content"] or "")
            if cost > budget:
                keep_from_idx = i + 1
                break
            budget -= cost
            keep_from_idx = i
        # Summarise rows[0:keep_from_idx]; keep rows[keep_from_idx:] raw.
        if keep_from_idx < self.summary_threshold:
            # Not enough to summarise — bail
            return False
        to_summarise = rows[:keep_from_idx]
        # Convert to dicts for the summariser
        chunk = [
            {"role": r["role"], "content": r["content"]}
            for r in to_summarise
        ]
        summariser = self._summariser or _get_default_summariser()
        try:
            summary_text = summariser(chunk)
        except Exception as exc:
            logger.warning("summariser failed (skipping): %s", exc)
            return False
        if not summary_text or not summary_text.strip():
            logger.warning("summariser returned empty text")
            return False
        # Place the summary row just AFTER the newest summarised message
        # so chronological ordering remains meaningful for the loader.
        anchor_ts = to_summarise[-1]["created_at"]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain.messages (
                    conv_id, role, content, surface,
                    is_summary, streaming_done, created_at,
                    tokens_in, tokens_out, model
                ) VALUES (%s, %s, %s, %s, TRUE, TRUE, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tid, "system", summary_text.strip(), "summariser",
                    anchor_ts,
                    self.token_counter("\n".join(c["content"] or "" for c in chunk)),
                    self.token_counter(summary_text),
                    "summariser",
                ),
            )
            new_id = cur.fetchone()["id"]
            emit_event({
                "kind": "summary.created",
                "thread_id": str(tid),
                "message_id": new_id,
                "covered_count": len(to_summarise),
                "covered_from_id": to_summarise[0]["id"],
                "covered_to_id": to_summarise[-1]["id"],
            }, self.dsn, conn=conn)
        logger.info(
            "summarised %d msgs into msg id=%s for thread %s",
            len(to_summarise), new_id, tid,
        )
        return True

    def summarise_all(self, thread_id: uuid.UUID | str) -> int | None:
        """Eager full-thread summarisation — user-triggered context reset.

        Collapses every finalised, non-summary, non-pinned message in the
        thread into a single `is_summary=TRUE` row. After this, the loader's
        walk-backward stops at that summary on the next call.

        Returns the new summary row id, or None if there was nothing to
        compress (thread too small, summariser unavailable, etc.).
        """
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, role, content, created_at FROM brain.messages
                 WHERE conv_id = %s
                   AND pinned = FALSE
                   AND streaming_done = TRUE
                   AND is_summary = FALSE
                   AND NOT (role = 'assistant' AND (
                            content LIKE '[bridge_error]%%'
                         OR content LIKE 'LLM request failed%%'
                         OR content LIKE 'Request timed out before%%'
                         OR content LIKE 'Context overflow: prompt too large%%'
                         OR content LIKE '%%agent returned status=None%%'
                         OR content = '[interrupted]'))
                 ORDER BY created_at ASC, id ASC
                """,
                (tid,),
            )
            rows = cur.fetchall()
        if len(rows) < 2:
            logger.info("summarise_all: thread %s has <2 messages, skipping", tid)
            return None
        chunk = [{"role": r["role"], "content": r["content"]} for r in rows]
        summariser = self._summariser or _get_default_summariser()
        try:
            summary_text = summariser(chunk)
        except Exception as exc:
            logger.warning("summarise_all: summariser failed: %s", exc)
            return None
        if not summary_text or not summary_text.strip():
            logger.warning("summarise_all: empty summary text")
            return None
        anchor_ts = rows[-1]["created_at"]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain.messages (
                    conv_id, role, content, surface,
                    is_summary, streaming_done, created_at,
                    tokens_in, tokens_out, model
                ) VALUES (%s, %s, %s, %s, TRUE, TRUE, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tid, "system", summary_text.strip(), "summariser",
                    anchor_ts,
                    self.token_counter("\n".join(c["content"] or "" for c in chunk)),
                    self.token_counter(summary_text),
                    "summariser",
                ),
            )
            new_id = cur.fetchone()["id"]
            emit_event({
                "kind": "summary.created",
                "thread_id": str(tid),
                "message_id": new_id,
                "covered_count": len(rows),
                "covered_from_id": rows[0]["id"],
                "covered_to_id": rows[-1]["id"],
                "manual": True,
            }, self.dsn, conn=conn)
        logger.info(
            "summarise_all: %d msgs compressed into msg id=%s for thread %s",
            len(rows), new_id, tid,
        )
        return new_id

    def _load_within_budget(self, tid: uuid.UUID, max_tokens: int) -> list[dict]:
        """The raw budget-walk loader (Phase 1 behaviour with summary anchors)."""
        budget = max_tokens
        pinned_rows: list[Message] = []
        recent_rows: list[Message] = []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM brain.messages
                 WHERE conv_id = %s
                   AND pinned = TRUE
                 ORDER BY created_at ASC, id ASC
                """,
                (tid,),
            )
            for row in cur.fetchall():
                pinned_rows.append(Message.from_row(row))
            cur.execute(
                """
                SELECT * FROM brain.messages
                 WHERE conv_id = %s
                   AND pinned = FALSE
                   AND streaming_done = TRUE
                   AND NOT (role = 'assistant' AND (
                            content LIKE '[bridge_error]%%'
                         OR content LIKE 'LLM request failed%%'
                         OR content LIKE 'Request timed out before%%'
                         OR content LIKE 'Context overflow: prompt too large%%'
                         OR content LIKE '%%agent returned status=None%%'
                         OR content = '[interrupted]'))
                 ORDER BY created_at DESC, id DESC
                """,
                (tid,),
            )
            for row in cur.fetchall():
                msg = Message.from_row(row)
                cost = self.token_counter(msg.content or "")
                if cost > budget:
                    break
                recent_rows.append(msg)
                budget -= cost
                # Summary anchors are terminal: nothing older needs loading.
                if msg.is_summary:
                    break
        recent_rows.reverse()
        ordered = pinned_rows + recent_rows
        return [self._to_llm_dict(m) for m in ordered]

    @staticmethod
    def _to_llm_dict(m: Message) -> dict:
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls is not None:
            d["tool_calls"] = m.tool_calls
        if m.role == "tool" and m.parent_msg_id is not None:
            d["tool_call_id"] = str(m.parent_msg_id)
        return d

    # ── diagnostics ──────────────────────────────────────────────────
    def message_count(self, conv_id: uuid.UUID | str) -> int:
        tid = uuid.UUID(str(conv_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM brain.messages WHERE conv_id = %s",
                (tid,),
            )
            return cur.fetchone()["n"]

    # ── surface bindings (Phase 3) ───────────────────────────────────
    def thread_by_name(
        self, user_id: str, name: str
    ) -> Thread | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM brain.conversations WHERE user_id = %s AND name = %s",
                (user_id, name),
            )
            row = cur.fetchone()
        return Thread.from_row(row) if row else None

    def get_active_thread(
        self,
        surface: str,
        surface_account: str,
        user_id: str = DEFAULT_USER,
        default_thread_name: str = "default",
        tg_topic_id: int | None = None,
    ) -> Thread:
        """Return the thread bound to (surface, surface_account[, tg_topic_id]).
        Creates a thread + binding on first call — idempotent.

        When `tg_topic_id` is provided (Telegram forum topic), the binding is
        scoped to that topic so different topics in the same chat map to
        different threads. None = DM / "General" topic = back-compat path."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*
                  FROM brain.surface_bindings sb
                  JOIN brain.conversations c ON c.id = sb.active_thread_id
                 WHERE sb.surface = %s AND sb.surface_account = %s
                   AND COALESCE(sb.tg_topic_id, -1) = COALESCE(%s::bigint, -1)
                """,
                (surface, surface_account, tg_topic_id),
            )
            row = cur.fetchone()
            if row:
                return Thread.from_row(row)

            # Nothing bound for this (surface, account, topic) — ensure
            # default thread exists, then bind.
            cur.execute(
                """
                INSERT INTO brain.conversations (id, user_id, name, kind)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, name) DO UPDATE
                    SET last_active_at = brain.conversations.last_active_at
                RETURNING *
                """,
                (uuid.uuid4(), user_id, default_thread_name, "general"),
            )
            thread_row = cur.fetchone()
            cur.execute(
                """
                INSERT INTO brain.surface_bindings
                    (surface, surface_account, user_id, active_thread_id, tg_topic_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (surface, surface_account, COALESCE(tg_topic_id, -1::bigint))
                DO UPDATE SET active_thread_id = EXCLUDED.active_thread_id,
                              updated_at = now()
                """,
                (surface, surface_account, user_id, thread_row["id"], tg_topic_id),
            )
        return Thread.from_row(thread_row)

    # ── per-thread tool overrides (per-thread "tool loadout") ────────
    def get_thread_tool_overrides(
        self, thread_id: uuid.UUID | str
    ) -> dict[str, bool]:
        """Return {tool_uuid -> enabled} for the thread. Missing = use the
        global default. Caller resolves merged state."""
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tool_uuid::text, enabled FROM brain.thread_tool_overrides WHERE thread_id = %s",
                (tid,),
            )
            return {r["tool_uuid"]: r["enabled"] for r in cur.fetchall()}

    def set_thread_tool_override(
        self,
        thread_id: uuid.UUID | str,
        tool_uuid: uuid.UUID | str,
        server_uuid: uuid.UUID | str,
        enabled: bool,
    ) -> None:
        tid = uuid.UUID(str(thread_id))
        toolid = uuid.UUID(str(tool_uuid))
        svrid = uuid.UUID(str(server_uuid))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain.thread_tool_overrides
                    (thread_id, tool_uuid, server_uuid, enabled)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (thread_id, tool_uuid) DO UPDATE
                    SET enabled = EXCLUDED.enabled,
                        updated_at = now()
                """,
                (tid, toolid, svrid, enabled),
            )

    def apply_thread_overrides_to_namespace(
        self,
        thread_id: uuid.UUID | str,
        namespace_uuid: str,
    ) -> int:
        """Copy this thread's tool override intent into MetaMCP's
        namespace_tool_mappings so OpenClaw sees the right toolset on the
        next invocation.  Single-user serialisation makes this safe.

        Returns count of rows touched.

        The strategy: for tools the thread has explicit overrides on, UPSERT
        their (ACTIVE/INACTIVE) status. For tools NOT in the thread's
        overrides, ensure they're ACTIVE (default-on for new threads).
        """
        tid = uuid.UUID(str(thread_id))
        overrides = self.get_thread_tool_overrides(tid)
        touched = 0
        with self._connect() as conn, conn.cursor() as cur:
            # For overridden tools, UPSERT explicit status
            for tool_uuid_str, enabled in overrides.items():
                # Look up server_uuid from the override row (we stored it)
                cur.execute(
                    "SELECT server_uuid FROM brain.thread_tool_overrides WHERE thread_id = %s AND tool_uuid = %s",
                    (tid, uuid.UUID(tool_uuid_str)),
                )
                row = cur.fetchone()
                if not row:
                    continue
                server_uuid = row["server_uuid"]
                status = "ACTIVE" if enabled else "INACTIVE"
                cur.execute(
                    f"""
                    INSERT INTO public.namespace_tool_mappings
                        (namespace_uuid, tool_uuid, mcp_server_uuid, status)
                    VALUES (%s, %s, %s, %s::mcp_server_status)
                    ON CONFLICT (namespace_uuid, tool_uuid) DO UPDATE
                        SET status = EXCLUDED.status
                    """,
                    (namespace_uuid, uuid.UUID(tool_uuid_str), server_uuid, status),
                )
                touched += 1
            # For all tools NOT explicitly overridden, ensure ACTIVE.
            # (Stale INACTIVEs left by a previous thread's loadout would
            # otherwise persist.)
            if overrides:
                placeholders = ",".join(["%s"] * len(overrides))
                cur.execute(
                    f"""
                    UPDATE public.namespace_tool_mappings
                       SET status = 'ACTIVE'::mcp_server_status
                     WHERE namespace_uuid = %s
                       AND status = 'INACTIVE'
                       AND tool_uuid NOT IN ({placeholders})
                    """,
                    (namespace_uuid, *[uuid.UUID(k) for k in overrides.keys()]),
                )
                touched += cur.rowcount
            else:
                # Thread has no overrides → all-on
                cur.execute(
                    """
                    UPDATE public.namespace_tool_mappings
                       SET status = 'ACTIVE'::mcp_server_status
                     WHERE namespace_uuid = %s
                       AND status = 'INACTIVE'
                    """,
                    (namespace_uuid,),
                )
                touched += cur.rowcount
        return touched

    def set_active_thread(
        self,
        surface: str,
        surface_account: str,
        thread_id: uuid.UUID | str,
        user_id: str = DEFAULT_USER,
        tg_topic_id: int | None = None,
    ) -> None:
        tid = uuid.UUID(str(thread_id))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain.surface_bindings
                    (surface, surface_account, user_id, active_thread_id, tg_topic_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (surface, surface_account, COALESCE(tg_topic_id, -1::bigint))
                DO UPDATE SET active_thread_id = EXCLUDED.active_thread_id,
                              user_id = EXCLUDED.user_id,
                              updated_at = now()
                """,
                (surface, surface_account, user_id, tid, tg_topic_id),
            )
