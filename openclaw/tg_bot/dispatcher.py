"""Pure-Python Telegram update dispatcher.

Separated from the I/O layer (`bot.py`) so the command-handling logic
can be exercised against synthetic updates without hitting the Telegram
API or OpenClaw.  The dispatcher returns a list of outgoing messages;
the bot loop is responsible for actually delivering them.
"""
from __future__ import annotations

import html
import json as _json
import logging
import re
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable

from openclaw.brain_store import BrainStore, Thread


logger = logging.getLogger("openclaw.tg_bot.dispatcher")


SURFACE = "telegram"

_VIA_PAT = re.compile(r"_via ([^_\n]+)_")

_BRIDGE_LAST_ROUTE_URL = "http://127.0.0.1:8095/last_route"


def _bridge_routed_model() -> str | None:
    """Query the infer bridge's /last_route endpoint for the model that
    actually ran the most recent inference. Falls back to None if the
    bridge is unreachable — caller decides what to display in that case."""
    try:
        with urllib.request.urlopen(_BRIDGE_LAST_ROUTE_URL, timeout=2) as r:
            data = _json.loads(r.read().decode())
        return data.get("target")
    except Exception as exc:
        logger.debug("bridge /last_route unreachable: %s", exc)
        return None


def _fmt_tokens(n) -> str | None:
    """Compact token count: 12345 → '12.3k', 850 → '850'. None if not a +int."""
    if not isinstance(n, int) or n <= 0:
        return None
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# A stalled / timed-out model_call almost always means the thread grew too large
# and jammed the single inference slot (see reference_openclaw_jammed_slot_recovery).
# Surface the one-tap fix to the owner instead of a bare error.
_JAM_SIGNS = ("timed out", "timeout", "stall", "bridge_error", "deadline",
              "504", "econnreset", "no response", "status=none")


def _jam_hint(err) -> str:
    low = str(err or "").lower()
    if any(s in low for s in _JAM_SIGNS):
        return "\n\n💡 The thread may be too long and stalled the model. Send /new to start a fresh thread."
    return ""


# Soft interim nudge: if a turn is STILL running after this many seconds, tell the
# owner /new is an option — without changing any timeout. Set ABOVE the ~180s
# cold-load floor so normal turns + cold loads finish first; only genuinely
# dragging turns (heading toward the 600s budget) get it. 0 disables.
NUDGE_DELAY_S = 210


def _format_assistant_reply(
    reply: str, fallback_model: str | None,
    prompt_tokens=None, context_limit=None,
) -> tuple[str, str]:
    """HTML-escape the reply, italicize `_via X_` markers, and append a footer
    with the model and the context usage (`ctx: <prompt>/<limit>`) so the owner
    can track how full the window is. Prefers the bridge's actually-routed
    model; falls back to OpenClaw's reported model."""
    body = html.escape(reply)
    body = _VIA_PAT.sub(lambda m: f"<i>via {m.group(1)}</i>", body)
    parts: list[str] = []
    model = _bridge_routed_model() or fallback_model
    if model:
        parts.append(f"model: {html.escape(model.split('/', 1)[-1])}")
    used, limit = _fmt_tokens(prompt_tokens), _fmt_tokens(context_limit)
    if used and limit:
        parts.append(f"ctx: {used}/{limit}")
    elif used:
        parts.append(f"ctx: {used}")
    if parts:
        body += "\n<i>· " + " · ".join(parts) + "</i>"
    return body, "HTML"


@dataclass
class Outbox:
    """One outgoing Telegram message.  The bot loop maps this to
    `sendMessage` against the live API."""
    chat_id: int
    text: str
    parse_mode: str | None = None
    reply_to_message_id: int | None = None
    message_thread_id: int | None = None   # forum topic id (None = General/DM)


@dataclass
class ChatTurnResult:
    """Subset of brain_wrapper.chat_turn's dict we actually need —
    keeping it explicit makes the test fakes obvious."""
    ok: bool
    reply: str
    error_detail: str | None = None


ChatTurnFn = Callable[..., dict]


HELP_TEXT = (
    "Sentinel sidecar bot — shared-brain.\n"
    "\n"
    "Each forum topic in this group is its own conversation thread, shared "
    "with web, Android, and the Comet side panel. Manage threads with "
    "Telegram's native topic controls:\n"
    "  • New topic → new thread\n"
    "  • Rename topic → renames the thread\n"
    "  • Close topic → archives the thread\n"
    "Threads you start elsewhere appear here as new topics automatically.\n"
    "\n"
    "<b>Threads</b>\n"
    "/new — fresh thread / reset context here (also /reset, /clear)\n"
    "/compact — summarise this thread to shrink its context\n"
    "/threads — list threads (★ = this topic)\n"
    "/rename &lt;name&gt; — rename this thread\n"
    "\n"
    "<b>Inference</b>\n"
    "/model — show the live inference model\n"
    "/status — health: inference + brain store\n"
    "/usage — context/token usage for this thread\n"
    "/think — toggle step-by-step reasoning mode\n"
    "/stop — abort the in-flight turn\n"
    "\n"
    "<b>Misc</b>\n"
    "/whoami — your TG id + this topic's thread\n"
    "/help — this message\n"
    "\n"
    "In a 1:1 DM there are no topics, so /new is how you reset.\n"
    "Anything else: chat with OpenClaw in this topic's thread."
)


class TelegramDispatcher:
    def __init__(
        self,
        store: BrainStore,
        chat_turn_fn: ChatTurnFn,
        owner_id: int | None = None,
        owner_user: str = "azfar",
        tg_token: str | None = None,
        deliver_fn=None,
    ) -> None:
        self.store = store
        self.chat_turn_fn = chat_turn_fn
        self.owner_id = owner_id
        self.owner_user = owner_user
        # Optional Bot API token — when provided, /rename also calls
        # editForumTopic so the TG topic title matches.
        self.tg_token = tg_token
        # When set, chat turns run in a WORKER THREAD and deliver via this
        # callback, so the bot's message loop stays free to receive /stop (and
        # quick commands) mid-turn. None → synchronous (unit tests / fallback).
        self.deliver_fn = deliver_fn
        # Per-thread "reasoning mode" (/think). When a thread id is in here, the
        # next turns get a non-persisted directive nudging step-by-step reasoning.
        self._think_on: set[str] = set()
        # monotonic timestamp of the last /stop — a turn whose kill lands after
        # its own start reports a clean "Stopped" instead of an error.
        self._stop_at: float = 0.0

    # ── public entry ───────────────────────────────────────────────
    def handle_update(self, update: dict) -> list[Outbox]:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return []
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        chat_id = chat.get("id")
        from_id = sender.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or from_id is None:
            return []

        # ACL — owner-only.  No multi-user in V4 Mode A.
        if self.owner_id is not None and from_id != self.owner_id:
            chat_type = chat.get("type") or ""
            logger.warning(
                "rejecting message from non-owner from_id=%s chat_type=%s",
                from_id,
                chat_type,
            )
            # Only surface "access denied" when a real user DMs the bot 1:1.
            # In groups/supergroups every non-owner message (other members, the
            # ops/middleware bot, GroupAnonymousBot, channel auto-forwards,
            # service events) would otherwise trigger a reply — that's the
            # "access denied keeps appearing" spam. Ignore those silently.
            if chat_type == "private" and not sender.get("is_bot"):
                return [Outbox(chat_id, "access denied")]
            return []

        # Forum topic context — None for DMs and the "General" topic of a forum.
        # is_topic_message + message_thread_id are set only when the user is
        # actually posting inside a sub-topic; the General topic doesn't carry them.
        topic_id = msg.get("message_thread_id") if msg.get("is_topic_message") else None

        # Topic-created system event: capture the title so we can name the
        # brain_store thread after it instead of just "topic-N".
        ftc = msg.get("forum_topic_created")
        if ftc and topic_id is not None:
            self._on_topic_created(chat_id, topic_id, ftc.get("name", f"topic-{topic_id}"))
            return []
        # Topic-closed: archive the bound thread (mirror of outbound archive
        # → closeForumTopic). Reopen is the symmetric undo.
        if msg.get("forum_topic_closed") is not None and topic_id is not None:
            self._on_topic_closed(chat_id, topic_id)
            return []
        if msg.get("forum_topic_reopened") is not None and topic_id is not None:
            self._on_topic_reopened(chat_id, topic_id)
            return []
        if msg.get("forum_topic_edited") is not None and topic_id is not None:
            new_name = (msg.get("forum_topic_edited") or {}).get("name")
            if new_name:
                self._on_topic_renamed(chat_id, topic_id, new_name)
            return []

        if text.startswith("/"):
            return self._handle_command(chat_id, from_id, text, topic_id=topic_id)
        if not text:
            return [Outbox(chat_id, "I only handle text messages right now.", message_thread_id=topic_id)]
        return self._handle_chat(chat_id, from_id, text, msg.get("message_id"), topic_id=topic_id)

    def _on_topic_created(self, chat_id: int, topic_id: int, name: str) -> None:
        """When a TG forum topic is created, create a matching brain_store
        thread + binding so subsequent messages in that topic route there."""
        # Sanitize name to fit our thread-name rules (1-40 chars, alnum + -_ space)
        safe = "".join(c for c in name if c.isalnum() or c in "-_ ")[:40].strip() or f"topic-{topic_id}"
        # If brain_store already has a thread with this name, just bind to it.
        existing = self.store.thread_by_name(self.owner_user, safe)
        if existing:
            self.store.set_active_thread(
                surface=SURFACE,
                surface_account=str(chat_id),
                thread_id=existing.id,
                user_id=self.owner_user,
                tg_topic_id=topic_id,
            )
            return
        # Otherwise create a new thread and bind. UNIQUE collision is unlikely
        # but possible — fall back to a suffixed name on conflict.
        try:
            thread = self.store.create_thread(user_id=self.owner_user, name=safe)
        except Exception:
            thread = self.store.create_thread(
                user_id=self.owner_user, name=f"{safe}-{topic_id}",
            )
        self.store.set_active_thread(
            surface=SURFACE,
            surface_account=str(chat_id),
            thread_id=thread.id,
            user_id=self.owner_user,
            tg_topic_id=topic_id,
        )
        logger.info("topic created: chat=%s topic=%s name=%r → thread=%s",
                    chat_id, topic_id, safe, thread.id)

    def _on_topic_closed(self, chat_id: int, topic_id: int) -> None:
        """TG topic was closed by the user — archive the bound brain thread.
        The outbound side (brain archive → closeForumTopic) is in topic_sync;
        this is the inbound counterpart. Idempotent."""
        thread = self.store.get_active_thread(
            surface=SURFACE, surface_account=str(chat_id),
            user_id=self.owner_user, tg_topic_id=topic_id,
        )
        # Already archived? brain_store.archive is idempotent — fires the
        # event again, but the outbound listener's close is no-op on closed.
        self.store.archive(thread.id)
        logger.info("topic closed: chat=%s topic=%s → archived thread=%s",
                    chat_id, topic_id, thread.id)

    def _on_topic_reopened(self, chat_id: int, topic_id: int) -> None:
        """TG topic was reopened — clear the archived_at flag on the bound
        thread. brain_store doesn't expose a 'reopen' method yet, so do it
        with a small direct SQL update here."""
        thread = self.store.get_active_thread(
            surface=SURFACE, surface_account=str(chat_id),
            user_id=self.owner_user, tg_topic_id=topic_id,
        )
        import psycopg
        with psycopg.connect(self.store.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain.conversations SET archived_at = NULL WHERE id = %s",
                (thread.id,),
            )
        logger.info("topic reopened: chat=%s topic=%s → unarchived thread=%s",
                    chat_id, topic_id, thread.id)

    def _on_topic_renamed(self, chat_id: int, topic_id: int, new_name: str) -> None:
        """TG topic was renamed — propagate the new title to the bound
        brain_store thread. Skip if the rename came from our own /rename
        (we already updated brain_store before calling editForumTopic)."""
        safe = "".join(c for c in new_name if c.isalnum() or c in "-_ ")[:40].strip()
        if not safe:
            return
        thread = self.store.get_active_thread(
            surface=SURFACE, surface_account=str(chat_id),
            user_id=self.owner_user, tg_topic_id=topic_id,
        )
        if thread.name == safe:
            return  # already in sync (echo from our own /rename)
        # If the new name collides with another thread, suffix it
        target = safe
        if self.store.thread_by_name(self.owner_user, target) is not None:
            target = f"{safe}-{topic_id}"
        import psycopg
        with psycopg.connect(self.store.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain.conversations SET name = %s WHERE id = %s",
                (target, thread.id),
            )
        logger.info("topic renamed: chat=%s topic=%s → thread=%s name=%r",
                    chat_id, topic_id, thread.id, target)

    # ── typing indicator ───────────────────────────────────────────
    def _send_typing(self, chat_id: int, topic_id: int | None) -> None:
        """Fire one Telegram 'typing' chat action. Best-effort, never raises."""
        if not self.tg_token:
            return
        params: dict = {"chat_id": chat_id, "action": "typing"}
        if topic_id is not None:
            params["message_thread_id"] = topic_id
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.tg_token}/sendChatAction",
                data=_json.dumps(params).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass  # a typing hiccup must never affect the turn

    @contextmanager
    def _typing_indicator(self, chat_id: int, topic_id: int | None):
        """Keep 'typing…' alive in Telegram for the duration of a turn.

        Telegram's typing action lasts ~5s, so a single send isn't enough for a
        5-30s turn — re-send every 4s on a daemon thread until the turn ends.
        """
        if not self.tg_token:
            yield
            return
        stop = threading.Event()

        def _loop() -> None:
            while not stop.is_set():
                self._send_typing(chat_id, topic_id)
                stop.wait(4.0)

        self._send_typing(chat_id, topic_id)   # immediate, before the thread spins
        t = threading.Thread(target=_loop, name="tg-typing", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()

    # ── chat path ──────────────────────────────────────────────────
    def _handle_chat(
        self, chat_id: int, from_id: int, text: str, tg_msg_id: int | None,
        topic_id: int | None = None,
    ) -> list[Outbox]:
        # Forum model: each Telegram forum topic maps to its own brain thread;
        # message_thread_id (topic_id) carries which thread the message belongs
        # to. The General topic and 1:1 DMs (topic_id is None) fall back to the
        # shared "default" thread. Threads are created/renamed/closed via TG's
        # native forum controls — no custom commands.
        thread = self.store.get_active_thread(
            surface=SURFACE,
            surface_account=str(chat_id),
            user_id=self.owner_user,
            tg_topic_id=topic_id,
            default_thread_name=f"topic-{topic_id}" if topic_id is not None else "default",
        )
        # Echo guard: when the bridge mirrors a Mini App / Tauri message to
        # TG via Telethon, the bot's getUpdates catches it ~100-500ms later.
        # Without this check we'd run chat_turn a second time on the same
        # content, generating duplicate assistant rows. brain_store already
        # has the canonical row from the originating surface — skip silently.
        if self._is_mirror_echo(thread.id, text):
            logger.info(
                "skipping mirror echo (chat=%s, thread=%s, msg_len=%d)",
                chat_id, thread.id, len(text),
            )
            return []
        # Run the (slow, 5-30s) turn in a worker thread so the message loop
        # stays free to receive /stop and quick commands mid-turn; the worker
        # delivers the reply when done. No deliver_fn (unit tests) → synchronous.
        if self.deliver_fn is None:
            return self._run_turn(chat_id, thread, text, tg_msg_id, topic_id)

        def _worker() -> None:
            # Soft interim nudge — fires once if the turn outlives NUDGE_DELAY_S;
            # cancelled the instant the turn returns. `done` guards the rare
            # fire-vs-finish race so a just-completed turn can't still nudge.
            done = threading.Event()
            nudge_timer = None
            if NUDGE_DELAY_S > 0:
                def _nudge() -> None:
                    if done.is_set():
                        return
                    try:
                        self.deliver_fn([Outbox(
                            chat_id,
                            "⏳ Still working… if it hangs, send /new to start a fresh thread.",
                            reply_to_message_id=tg_msg_id, message_thread_id=topic_id)])
                    except Exception:
                        logger.exception("nudge deliver failed")
                nudge_timer = threading.Timer(NUDGE_DELAY_S, _nudge)
                nudge_timer.daemon = True
                nudge_timer.start()
            try:
                outs = self._run_turn(chat_id, thread, text, tg_msg_id, topic_id)
            except Exception as e:  # noqa: BLE001 — a worker crash must still reply
                logger.exception("chat-turn worker crashed: %s", e)
                outs = [Outbox(chat_id, f"⚠ turn error: {e}{_jam_hint(e)}",
                               reply_to_message_id=tg_msg_id, message_thread_id=topic_id)]
            finally:
                done.set()
                if nudge_timer is not None:
                    nudge_timer.cancel()
            try:
                self.deliver_fn(outs)
            except Exception:
                logger.exception("deliver from worker failed")

        threading.Thread(target=_worker, name="chat-turn", daemon=True).start()
        return []

    def _run_turn(self, chat_id, thread, text, tg_msg_id, topic_id) -> list[Outbox]:
        """Execute one chat turn and return the reply Outbox(es). Runs either
        inline (sync fallback) or inside the worker thread spawned above."""
        turn_start = time.monotonic()
        # Show "typing…" for the whole turn so the owner can tell it's working.
        enforce = None
        if str(thread.id) in self._think_on:
            enforce = {"kind": "directive",
                       "text": "[Reason carefully step-by-step before answering.]"}
        # DM-continuity: a 1:1 DM (chat_id > 0) whose thread ALSO has a group
        # forum-topic binding is tagged 'telegram-dm' so the cross-surface mirror
        # echoes this turn INTO that topic — the user message via Telethon (as the
        # owner) and the reply via the Bot API — giving the private DM and the
        # Sentinel Suite topic one shared, visible conversation. 'telegram-dm' is
        # DISTINCT FROM 'telegram', so _is_mirror_echo suppresses the round-trip
        # (no reply loop). DM-only threads keep plain 'telegram' (no mirror).
        turn_surface = SURFACE
        if chat_id > 0 and self._thread_has_topic_binding(thread.id):
            turn_surface = "telegram-dm"
        with self._typing_indicator(chat_id, topic_id):
            result = self.chat_turn_fn(
                thread_id=thread.id,
                user_msg=text,
                surface=turn_surface,
                store=self.store,
                enforce=enforce,
            )
        if not result.get("ok"):
            # If the owner /stop'd this turn, its agent subprocess was killed →
            # not-ok. Report a clean "Stopped" instead of a scary error.
            if self._stop_at >= turn_start:
                return [Outbox(chat_id, "⏹ Stopped.", reply_to_message_id=tg_msg_id,
                               message_thread_id=topic_id)]
            logger.error(
                "chat_turn not ok chat=%s thread=%s error=%r detail=%r model=%r dur=%sms",
                chat_id, thread.id, result.get("error"), result.get("error_detail"),
                result.get("model"), result.get("duration_ms"),
            )
            err = result.get('error_detail') or result.get('error') or 'unknown'
            return [Outbox(
                chat_id,
                f"⚠ chat_turn failed: {err}{_jam_hint(err)}",
                reply_to_message_id=tg_msg_id,
                message_thread_id=topic_id,
            )]
        reply = result.get("reply")
        if not reply or not reply.strip():
            # status=ok with ZERO payloads — most often a bare greeting where the
            # model fires a tool-search instead of answering. Don't show "(no
            # reply)"; answer the greeting.
            logger.info(
                "empty reply (0 payloads) chat=%s thread=%s model=%r — sending fallback",
                chat_id, thread.id, result.get("model"),
            )
            reply = "👋 Hey! I'm here — what can I help you with?"
        out_text, pm = _format_assistant_reply(
            reply, result.get("model"),
            prompt_tokens=result.get("prompt_tokens"),
            context_limit=result.get("context_limit"),
        )
        return [Outbox(chat_id, out_text, parse_mode=pm,
                       reply_to_message_id=tg_msg_id, message_thread_id=topic_id)]

    # Echo window: how recently a non-TG row must exist for an incoming TG
    # message to be considered an echo. Slack covers Telethon delivery +
    # bot getUpdates polling latency.
    _ECHO_WINDOW_SECONDS = 10

    def _is_mirror_echo(self, thread_id, text: str) -> bool:
        """True if a matching non-TG user row was inserted into this thread
        in the last _ECHO_WINDOW_SECONDS. Used to suppress double-processing
        of bridge-mirrored Mini App / Tauri messages that come back through
        the bot's poll."""
        try:
            import psycopg
            with psycopg.connect(self.store.dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM brain.messages
                     WHERE conv_id = %s
                       AND role = 'user'
                       AND content = %s
                       AND surface IS DISTINCT FROM 'telegram'
                       AND created_at > now() - interval '%s seconds'
                     LIMIT 1
                    """,
                    (thread_id, text, self._ECHO_WINDOW_SECONDS),
                )
                return cur.fetchone() is not None
        except Exception as exc:
            # Be conservative: if the dedup query fails, allow the message
            # through so the user doesn't lose a real chat.
            logger.warning("echo-check query failed (allowing through): %s", exc)
            return False

    def _thread_has_topic_binding(self, thread_id) -> bool:
        """True if this thread is bound to a Telegram forum *topic*
        (tg_topic_id set) — i.e. there's a group topic to mirror a DM turn
        into. Used to decide whether a 1:1 DM turn should be tagged
        'telegram-dm' (mirror-eligible) or plain 'telegram' (no mirror)."""
        try:
            import psycopg
            with psycopg.connect(self.store.dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM brain.surface_bindings
                     WHERE surface = 'telegram'
                       AND active_thread_id = %s
                       AND tg_topic_id IS NOT NULL
                     LIMIT 1
                    """,
                    (thread_id,),
                )
                return cur.fetchone() is not None
        except Exception as exc:
            logger.warning("topic-binding check failed (treating as none): %s", exc)
            return False

    # ── command dispatch ───────────────────────────────────────────
    def _handle_command(self, chat_id: int, from_id: int, text: str,
                        topic_id: int | None = None) -> list[Outbox]:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip("/").split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "start":
            return self._cmd_start(chat_id, topic_id)
        if cmd == "help":
            return [Outbox(chat_id, HELP_TEXT, parse_mode="HTML", message_thread_id=topic_id)]
        if cmd == "threads":
            return self._cmd_threads(chat_id, topic_id)
        if cmd == "whoami":
            return self._cmd_whoami(chat_id, from_id, topic_id)
        if cmd in ("new", "reset", "clear"):
            return self._cmd_new(chat_id, topic_id)
        if cmd == "compact":
            return self._cmd_compact(chat_id, topic_id)
        if cmd == "rename":
            return self._cmd_rename(chat_id, topic_id, arg)
        if cmd in ("model", "models"):
            return self._cmd_model(chat_id, topic_id)
        if cmd == "status":
            return self._cmd_status(chat_id, topic_id)
        if cmd in ("usage", "context", "ctx"):
            return self._cmd_usage(chat_id, topic_id)
        if cmd == "think":
            return self._cmd_think(chat_id, topic_id, arg)
        if cmd == "stop":
            return self._cmd_stop(chat_id, topic_id)
        # Thread rename/archive are also handled by TG's native forum topic
        # controls (see _on_topic_* handlers).
        return [Outbox(chat_id, f"unknown command: /{cmd}. Try /help.", message_thread_id=topic_id)]

    def _cmd_start(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        thread = self.store.get_active_thread(
            surface=SURFACE,
            surface_account=str(chat_id),
            user_id=self.owner_user,
            tg_topic_id=topic_id,
        )
        return [Outbox(
            chat_id,
            f"Sentinel sidecar ready. This topic's thread: <b>{_escape(thread.name)}</b>.\n"
            "Create a new TG topic to start a new thread, or /help for commands.",
            parse_mode="HTML",
            message_thread_id=topic_id,
        )]

    def _cmd_threads(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        active = self.store.get_active_thread(
            surface=SURFACE,
            surface_account=str(chat_id),
            user_id=self.owner_user,
            tg_topic_id=topic_id,
        )
        threads = self.store.list_threads(user_id=self.owner_user)
        if not threads:
            return [Outbox(chat_id, "No threads yet.", message_thread_id=topic_id)]
        lines = ["<b>Threads</b>"]
        for t in threads:
            mark = "★" if t.id == active.id else "·"
            lines.append(f"  {mark} {_escape(t.name)} ({_escape(t.kind)})")
        return [Outbox(chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=topic_id)]

    def _cmd_whoami(self, chat_id: int, from_id: int, topic_id: int | None = None) -> list[Outbox]:
        thread = self.store.get_active_thread(
            surface=SURFACE,
            surface_account=str(chat_id),
            user_id=self.owner_user,
            tg_topic_id=topic_id,
        )
        topic_str = f"\n<b>topic_id:</b> {topic_id}" if topic_id is not None else ""
        return [Outbox(
            chat_id,
            f"<b>tg_user_id:</b> {from_id}\n"
            f"<b>chat_id:</b> {chat_id}{topic_str}\n"
            f"<b>user:</b> {_escape(self.owner_user)}\n"
            f"<b>active thread:</b> {_escape(thread.name)} ({thread.id})",
            parse_mode="HTML",
            message_thread_id=topic_id,
        )]

    def _active(self, chat_id: int, topic_id: int | None):
        return self.store.get_active_thread(
            surface=SURFACE, surface_account=str(chat_id),
            user_id=self.owner_user, tg_topic_id=topic_id,
        )

    def _cmd_compact(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        """Summarise the current thread to shrink its context footprint."""
        thread = self._active(chat_id, topic_id)
        try:
            new_id = self.store.summarise_all(thread.id)
        except Exception as e:
            return [Outbox(chat_id, f"⚠ compact failed: {_escape(str(e)[:200])}",
                           parse_mode="HTML", message_thread_id=topic_id)]
        if new_id is None:
            return [Outbox(chat_id, "Nothing to compact — thread is already lean.",
                           message_thread_id=topic_id)]
        return [Outbox(chat_id, f"🗜 Compacted <b>{_escape(thread.name)}</b> — older "
                       "messages summarised; context is leaner now.",
                       parse_mode="HTML", message_thread_id=topic_id)]

    def _cmd_rename(self, chat_id: int, topic_id: int | None, arg: str) -> list[Outbox]:
        """Rename the current thread (and the TG topic, if this is one)."""
        name = "".join(c for c in arg if c.isalnum() or c in "-_ ")[:40].strip()
        if not name:
            return [Outbox(chat_id, "Usage: <code>/rename &lt;new name&gt;</code>",
                           parse_mode="HTML", message_thread_id=topic_id)]
        thread = self._active(chat_id, topic_id)
        self.store.rename_thread(thread.id, name)
        # Mirror to the TG forum topic title so the surfaces stay in sync.
        if topic_id is not None and self.tg_token:
            try:
                self._tg_post("editForumTopic", {
                    "chat_id": chat_id, "message_thread_id": topic_id, "name": name,
                })
            except Exception:
                pass
        return [Outbox(chat_id, f"✏️ Renamed to <b>{_escape(name)}</b>.",
                       parse_mode="HTML", message_thread_id=topic_id)]

    def _cmd_model(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        """Show the live inference model/backend (from infer-bridge)."""
        st = self._bridge_infer_status()
        if not st:
            return [Outbox(chat_id, "Inference bridge unreachable (:8095).",
                           message_thread_id=topic_id)]
        model = (st.get("model") or "?").split("/", 1)[-1]
        loaded = st.get("loaded") or []
        busy = "busy" if st.get("active") else "idle"
        extra = f"\n<i>loaded: {_escape(', '.join(loaded))}</i>" if loaded else ""
        return [Outbox(chat_id, f"🧠 <b>{_escape(model)}</b> · {busy}{extra}",
                       parse_mode="HTML", message_thread_id=topic_id)]

    def _cmd_status(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        """Quick health snapshot — inference + brain store."""
        st = self._bridge_infer_status()
        infer = (f"🟢 {_escape((st.get('model') or '?').split('/', 1)[-1])} "
                 f"({'busy' if st.get('active') else 'idle'})") if st else "🔴 unreachable"
        try:
            self.store.message_count(self._active(chat_id, topic_id).id)
            db = "🟢 connected"
        except Exception:
            db = "🔴 error"
        return [Outbox(chat_id,
                       f"<b>Status</b>\n• inference: {infer}\n• brain store: {db}",
                       parse_mode="HTML", message_thread_id=topic_id)]

    def _cmd_usage(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        """Token footprint of the current thread vs the context window."""
        thread = self._active(chat_id, topic_id)
        msgs = self.store.message_count(thread.id)
        used = None
        try:
            from openclaw.brain_wrapper import format_history_for_openclaw
            from openclaw.tokenizer import count_tokens
            history = self.store.load_for_llm(thread.id, max_tokens=120_000)
            used = count_tokens(format_history_for_openclaw(history))
        except Exception:
            pass
        limit_k = "65.5k"
        line = f"<b>Usage — {_escape(thread.name)}</b>\n• messages: {msgs}"
        if used is not None:
            line += f"\n• context: ~{used / 1000:.1f}k / {limit_k}"
            if used > 45_000:
                line += "\n• ⚠ getting full — consider /compact or /new"
        return [Outbox(chat_id, line, parse_mode="HTML", message_thread_id=topic_id)]

    def _cmd_think(self, chat_id: int, topic_id: int | None, arg: str) -> list[Outbox]:
        """Toggle step-by-step reasoning mode for this thread."""
        thread = self._active(chat_id, topic_id)
        key = str(thread.id)
        want = arg.strip().lower()
        if want in ("on", "1", "yes"):
            on = True
        elif want in ("off", "0", "no"):
            on = False
        else:
            on = key not in self._think_on   # toggle
        if on:
            self._think_on.add(key)
            msg = "🧩 Reasoning mode <b>ON</b> — I'll think step-by-step (slower, more deliberate)."
        else:
            self._think_on.discard(key)
            msg = "⚡ Reasoning mode <b>OFF</b> — back to direct answers."
        return [Outbox(chat_id, msg, parse_mode="HTML", message_thread_id=topic_id)]

    def _cmd_stop(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        """Abort an in-flight agent turn by killing its WSL subprocess. The
        worker running that turn sees the kill, and (because _stop_at is now
        set) reports a clean "⏹ Stopped." rather than an error."""
        import subprocess
        self._stop_at = time.monotonic()
        try:
            r = subprocess.run(
                ["wsl.exe", "-d", "Ubuntu-24.04", "--", "pkill", "-f", "dist/index.js agent"],
                capture_output=True, text=True, timeout=10,
            )
            stopped = r.returncode == 0   # pkill returns 0 if it killed something
        except Exception as e:
            return [Outbox(chat_id, f"⚠ stop failed: {_escape(str(e)[:160])}",
                           parse_mode="HTML", message_thread_id=topic_id)]
        # When something was killed, the worker delivers the "⏹ Stopped." — keep
        # this ack quiet to avoid a double message. When nothing ran, say so.
        if stopped:
            return []
        return [Outbox(chat_id, "Nothing was running.", message_thread_id=topic_id)]

    def _tg_post(self, method: str, params: dict) -> None:
        """POST a Telegram Bot API call with the dispatcher's token."""
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.tg_token}/{method}",
            data=_json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=8).read()

    def _bridge_infer_status(self) -> dict | None:
        """GET infer-bridge /infer_status → {active, model, blocked, loaded}."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:8095/infer_status", timeout=3) as r:
                return _json.loads(r.read().decode())
        except Exception:
            return None

    def _cmd_new(self, chat_id: int, topic_id: int | None = None) -> list[Outbox]:
        """Start a fresh conversation thread and switch this chat/topic to it.

        This is the reset path for 1:1 DMs, which (unlike the forum group)
        have no topics to spin up a new thread with. The previous thread is
        preserved — not deleted — so it stays recoverable via /threads or the
        Mini App; OpenClaw just starts the next turn from a clean context."""
        from datetime import datetime
        stamp = datetime.now().strftime("%m%d-%H%M%S")
        base = f"chat {stamp}"
        try:
            thread = self.store.create_thread(user_id=self.owner_user, name=base)
        except Exception:
            thread = self.store.create_thread(
                user_id=self.owner_user, name=f"{base}-{chat_id}",
            )
        self.store.set_active_thread(
            surface=SURFACE,
            surface_account=str(chat_id),
            thread_id=thread.id,
            user_id=self.owner_user,
            tg_topic_id=topic_id,
        )
        logger.info("reset: chat=%s topic=%s -> fresh thread=%s (%s)",
                    chat_id, topic_id, thread.id, thread.name)
        return [Outbox(
            chat_id,
            f"✨ Fresh thread started — <b>{_escape(thread.name)}</b>.\n"
            "OpenClaw now works from a clean context. Your previous "
            "conversation is kept (not deleted) — find it via /threads or the "
            "Mini App.",
            parse_mode="HTML",
            message_thread_id=topic_id,
        )]


# ── helpers ─────────────────────────────────────────────────────────
def _escape(text: str) -> str:
    """Telegram HTML escape — & < > only, per
    https://core.telegram.org/bots/api#html-style"""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
