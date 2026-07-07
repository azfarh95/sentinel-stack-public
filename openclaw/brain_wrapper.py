"""OpenClaw integration wrapper (Phase 2 + Phase 7.5 stateless refactor).

`chat_turn(thread_id, user_msg, surface)` is the single entry point every
client surface calls. It:

  1. Persists the user message via brain_store.append.
  2. Reserves an assistant row (streaming_done=False).
  3. Loads bounded history from brain_store (Phase 7 summariser applies).
  4. Renders it as a preamble + appends the current user message.
  5. Spawns the OpenClaw one-shot CLI with a FRESH per-turn session-id —
     OpenClaw does NOT reuse session jsonls across turns. brain_store is
     the only history of record. See Appendix I of the shared-brain plan.
  6. Parses OpenClaw's `agent --json` payload.
  7. Finalises the assistant row with reply text + token meta.

The stateless refactor (Phase 7.5) eliminates the two-store divergence
that caused `body_bytes > 100KB` failures (memory:
`feedback_openclaw_stalled_model_call`).

Streaming is NOT yet wired — OpenClaw's CLI returns one JSON document per
turn, and the inference bridge doesn't surface per-token deltas to here.
Phase 5 (WebSocket push) handles cross-surface notification via a
different path.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import datetime
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

from openclaw.brain_store import BrainStore, Message
from openclaw.tokenizer import count_tokens


WSL_DISTRO = os.environ.get("SENTINEL_WSL_DISTRO", "Ubuntu-24.04")
OPENCLAW_CLI = "/home/azfar/.npm-global/lib/node_modules/openclaw/dist/index.js"
DEFAULT_TIMEOUT_S = 600     # OpenClaw's own default
HARD_TIMEOUT_S = 900        # subprocess kill-fence on top
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

logger = logging.getLogger("openclaw.brain_wrapper")

# Serialise OpenClaw invocations across EVERY process on the host. The embedded
# one-shot `agent` CLI we spawn collides with the persistent WSL gateway (and
# with a concurrent turn) over session ownership — surfacing as
# EmbeddedAttemptSessionTakeoverError. The web/Mini App bridge and the Telegram
# sidecar bot run as SEPARATE processes, so a module-level threading.Lock only
# serialises within one of them. A named kernel mutex serialises across all of
# them and is auto-released by the OS if a holder crashes (no stale-lock
# deadlock). Falls back to the in-process lock on non-Windows / if the OS
# primitive is unavailable.
_OPENCLAW_LOCK = threading.Lock()
_TURNSTILE_NAME = "Local\\SentinelOpenClawGatewayTurn"


def _init_named_mutex():
    """Create the host-wide named mutex once. Returns (kernel32, handle) or
    None to signal the in-process-lock fallback."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.ReleaseMutex.restype = wintypes.BOOL
        k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        h = k.CreateMutexW(None, False, _TURNSTILE_NAME)
        if not h:
            logger.warning(
                "CreateMutexW failed (err=%d) — falling back to in-process lock",
                ctypes.get_last_error(),
            )
            return None
        return (k, h)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("named mutex unavailable (%s) — in-process lock only", exc)
        return None


_MUTEX = _init_named_mutex()


@contextlib.contextmanager
def gateway_turnstile():
    """Block until this thread/process owns the gateway, then yield. A fresh
    generator frame per call keeps per-acquisition state local even though the
    mutex handle is shared module-wide."""
    if _MUTEX is not None:
        k, h = _MUTEX
        # INFINITE wait. WAIT_OBJECT_0 (0) and WAIT_ABANDONED (0x80) both leave
        # us owning the mutex; only WAIT_FAILED (0xFFFFFFFF) does not.
        rc = k.WaitForSingleObject(h, 0xFFFFFFFF)
        if rc != 0xFFFFFFFF:
            try:
                yield
            finally:
                k.ReleaseMutex(h)
            return
        logger.warning("WaitForSingleObject failed — falling back to in-process lock")
    with _OPENCLAW_LOCK:
        yield


# ── OpenClaw CLI invocation ─────────────────────────────────────────────
def _win_to_wsl(p: str) -> str:
    """C:\\Users\\x\\f.txt -> /mnt/c/Users/x/f.txt (drive-letter lowercased)."""
    return f"/mnt/{p[0].lower()}{p[2:].replace(os.sep, '/')}"


def openclaw_one_shot(
    session_id: str,
    message: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """Spawn one OpenClaw agent turn. Returns the parsed JSON payload.

    Raises subprocess.TimeoutExpired on hard timeout. Returns a dict with
    `_bridge_error=True` for non-zero exit / parse failure (mirrors
    comet-sidepanel's contract)."""
    # OpenClaw runs over `wsl.exe -- bash -lc <cmd>`. Inlining the message into
    # that command line is UNSAFE: Windows subprocess (list2cmdline) + wsl.exe
    # re-parsing strip shlex's POSIX single-quoting, so any backtick / $() / ;
    # in the message — or in the history preamble prepended to it — gets executed
    # by bash → "unexpected EOF while looking for matching `" and the whole turn
    # dies before the model is ever called (the 2026-06-16 code-content flake:
    # a single ``` fence in a thread poisoned every later turn in it). Fix: hand
    # the payload across the wsl boundary OUT-OF-BAND. The message bytes go to a
    # temp file; a tiny script file (written here, so its quoting is preserved on
    # disk) reads it into "$MSG" and execs node. The ONLY thing crossing the wsl
    # command line is `bash <plain-ascii-temp-path>` — no quotes, no content — so
    # there is nothing for the transport to mangle. (Verified: backticks / fences
    # / quotes / $ / \\ all round-trip byte-for-byte.)
    logger.info("agent turn session=%s msg_len=%d", session_id, len(message))
    msg_fd, msg_win = tempfile.mkstemp(suffix=".txt", prefix="oc_msg_")
    scr_fd, scr_win = tempfile.mkstemp(suffix=".sh", prefix="oc_run_")
    t0 = time.time()
    try:
        with os.fdopen(msg_fd, "w", encoding="utf-8", newline="") as f:
            f.write(message)
        # Script quoting lives ON DISK (untouched by the wsl transport): the path
        # is single-quoted; "$MSG" is double-quoted so bash expands but never
        # re-parses the value. session-id is a uuid; CLI path is fixed.
        script = (
            f"MSG=\"$(cat {shlex.quote(_win_to_wsl(msg_win))})\"\n"
            f"exec node {shlex.quote(OPENCLAW_CLI)} agent "
            f"--session-id {shlex.quote(session_id)} "
            f"--message \"$MSG\" "
            f"--json --timeout {int(timeout_s)}\n"
        )
        with os.fdopen(scr_fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        # `bash <path>` only — plain ascii temp path, no quotes (the temp dir has
        # no spaces on this host). The outer -lc supplies node's login PATH, which
        # the script's bash inherits.
        cmd = ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc",
               f"bash {_win_to_wsl(scr_win)}"]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=HARD_TIMEOUT_S,
            creationflags=_NO_WINDOW,
        )
    finally:
        for _p in (msg_win, scr_win):
            try:
                os.unlink(_p)
            except OSError:
                pass
    elapsed_ms = int((time.time() - t0) * 1000)

    if proc.returncode != 0:
        logger.error(
            "openclaw rc=%d stderr=%s", proc.returncode, proc.stderr[-1000:]
        )
        return {
            "_bridge_error": True,
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr[-2000:],
            "_elapsed_ms": elapsed_ms,
        }

    raw = proc.stdout.strip()
    if not raw:
        return {
            "_bridge_error": True,
            "stderr_tail": "empty stdout from openclaw agent",
            "_elapsed_ms": elapsed_ms,
        }

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: pull the last {…} block (OpenClaw occasionally emits
        # a banner line before the JSON in odd error paths)
        last_brace = raw.rfind("{")
        try:
            data = json.loads(raw[last_brace:])
        except Exception as exc:
            logger.error("JSON parse failed: %s head=%s", exc, raw[:400])
            return {
                "_bridge_error": True,
                "stderr_tail": f"JSON parse failed: {exc}",
                "_elapsed_ms": elapsed_ms,
                "raw_head": raw[:400],
            }
    data["_elapsed_ms"] = elapsed_ms
    return data


def extract_reply(turn: dict) -> dict:
    """Distill OpenClaw's verbose `agent --json` payload to the fields
    brain_wrapper cares about. Mirrors comet-sidepanel/bridge.py:extract_reply."""
    if turn.get("_bridge_error"):
        return {
            "ok": False,
            "error": "bridge_error",
            "detail": (turn.get("stderr_tail") or "")[-1200:],
            "duration_ms": turn.get("_elapsed_ms", 0),
        }
    result = turn.get("result") or {}
    payloads = result.get("payloads") or []
    text_parts = [p.get("text", "") for p in payloads if p.get("text")]
    media = [p.get("mediaUrl") for p in payloads if p.get("mediaUrl")]
    agent_meta = ((result.get("meta") or {}).get("agentMeta") or {})
    usage = agent_meta.get("usage") or {}
    status = turn.get("status")
    ok = status == "ok"
    return {
        "ok": ok,
        # Surface WHY a non-ok turn failed. Previously only `_bridge_error`
        # (rc!=0 / parse fail) carried an error; an agent that returned valid
        # JSON with status=error/timeout fell through with no detail and read as
        # "unknown" downstream (the chat_turn-failed-unknown papercut, 2026-06-05).
        "error": None if ok else (turn.get("error") or result.get("error") or f"agent_status:{status}"),
        "detail": None if ok else (
            turn.get("summary") or result.get("error") or turn.get("error")
            or f"agent returned status={status!r}, {len(payloads)} payload(s), no reply text"
        ),
        "reply": "\n\n".join(text_parts).strip(),
        "media": media,
        "session_id": agent_meta.get("sessionId"),
        "run_id": turn.get("runId"),
        "summary": turn.get("summary"),
        "duration_ms": (
            turn.get("_elapsed_ms")
            or (result.get("meta") or {}).get("durationMs")
        ),
        "model": agent_meta.get("model"),
        "provider": agent_meta.get("provider"),
        "usage": usage,
        "context_limit": agent_meta.get("contextTokens"),
        "prompt_tokens": agent_meta.get("promptTokens") or usage.get("input"),
        "completion_tokens": usage.get("output"),
    }


# ── chat_turn — the public Phase 2 entry point ──────────────────────────
def _build_enforce_directive(enforce: dict | None) -> str:
    """Translate an enforce spec into a one-line OpenClaw directive we prepend
    to the user message at invocation time. Kept out of the persisted user
    content so chat history stays clean. Kinds:
      {kind: 'directive', text: '...'}  → freeform (the /think reasoning nudge)
      {kind: 'skill', name: '...'}      → [Use skill: name]
      {kind: 'mcp', name: '...'}        → [Use the name service]
    """
    if not enforce:
        return ""
    kind = (enforce.get("kind") or "mcp").lower()
    if kind == "directive":
        return (enforce.get("text") or "").strip()
    name = (enforce.get("name") or "").strip()
    if not name:
        return ""
    if kind == "skill":
        return f"[Use skill: {name}]"
    return f"[Use the {name} service]"


# ── History formatter — brain_store → OpenClaw ─────────────────────────
# Cap each prior message's prose so a single bloated turn can't blow the
# preamble. Tool calls and tool results are summarised lightly so OpenClaw
# sees that they happened without dragging the full payload through.
_MAX_PER_MSG_CHARS  = 1800
_MAX_PREAMBLE_CHARS = 32_000

_ROLE_LABEL = {"user": "User", "assistant": "Assistant", "tool": "Tool", "system": "Note"}


def format_history_for_openclaw(history: list[dict]) -> str:
    """Render `BrainStore.load_for_llm` output as a single OpenClaw-friendly
    preamble. Aggressively trims so the result fits comfortably under the
    100KB OpenClaw stall threshold even with the ~50KB system prompt on top.

    `history` does NOT include the current user message — that's appended
    by the caller. Final shape:

        [Prior conversation in this thread]
        User: …
        Assistant: …
        Note (summary): …
        [End of prior conversation]
    """
    if not history:
        return ""
    lines: list[str] = ["[Prior conversation in this thread]"]
    for m in history:
        role = m.get("role", "user")
        label = _ROLE_LABEL.get(role, role.title())
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _MAX_PER_MSG_CHARS:
            content = content[:_MAX_PER_MSG_CHARS] + " […truncated]"
        # Summary rows are surfaced explicitly so the model treats them as
        # compressed memory rather than turn dialogue.
        if role == "system":
            label = "Summary"
        lines.append(f"{label}: {content}")
    lines.append("[End of prior conversation]")
    rendered = "\n\n".join(lines)
    if len(rendered) > _MAX_PREAMBLE_CHARS:
        # Pathological: walk back from newest until we fit, preserving the
        # closing marker. Drops oldest first — same shape as Phase 7's
        # rolling summariser but at preamble-format time.
        keep = lines[-2::-1]  # everything except header/footer in reverse
        out: list[str] = []
        size = 80  # header/footer overhead
        for ln in keep:
            if size + len(ln) > _MAX_PREAMBLE_CHARS:
                break
            out.append(ln)
            size += len(ln) + 2
        rendered = "\n\n".join(["[Prior conversation in this thread — older messages omitted]", *reversed(out), "[End of prior conversation]"])
    return rendered


def chat_turn_begin(
    thread_id: uuid.UUID | str,
    user_msg: str,
    surface: str,
    store: BrainStore | None = None,
) -> dict:
    """Synchronous prelude of a turn: sync tool overrides, persist the user
    message, and reserve the assistant row in streaming state.

    Split out of `chat_turn` so a surface can ACK the POST immediately (the
    user message + a "thinking" placeholder land via the brain_events WS push)
    and run the slow OpenClaw turn in the background via `chat_turn_finish`.
    This is what keeps /chat under Cloudflare's ~100s edge timeout (no 524).

    Returns `{thread_id, user_message_id, assistant_message_id}`.
    """
    store = store or BrainStore(token_counter=count_tokens)
    tid = uuid.UUID(str(thread_id))
    if store.get_thread(tid) is None:
        raise KeyError(f"thread {tid} does not exist; create_thread first")

    # Sync this thread's tool overrides → MetaMCP's Default namespace. Each
    # thread is its own "tool loadout"; tools with no explicit override
    # default to ACTIVE.
    try:
        store.apply_thread_overrides_to_namespace(
            tid, namespace_uuid="0a83b85b-24ea-4491-b24b-17104bc9bba0",
        )
    except Exception as exc:
        logger.warning("tool override sync failed (continuing): %s", exc)

    user_row = store.append(
        conv_id=tid,
        role="user",
        content=user_msg,
        surface=surface,
        tokens_in=count_tokens(user_msg),
    )
    asst_row = store.append(
        conv_id=tid,
        role="assistant",
        content="",
        surface="server",
        streaming_done=False,
    )
    return {
        "thread_id": str(tid),
        "user_message_id": user_row.id,
        "assistant_message_id": asst_row.id,
    }


def chat_turn_finish(
    thread_id: uuid.UUID | str,
    user_msg: str,
    assistant_message_id: int,
    store: BrainStore | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    enforce: dict | None = None,
    max_history_tokens: int = 6000,
) -> dict:
    """Slow remainder of a turn started by `chat_turn_begin`: build the
    history preamble, invoke OpenClaw (serialised host-wide via
    `gateway_turnstile`), and finalise the reserved assistant row. `finalize` emits a `message.complete`
    event on brain_events, so the WS push delivers the reply to every surface.

    Returns the same dict shape as `chat_turn` (minus user_message_id, which
    `chat_turn_begin` already returned).
    """
    store = store or BrainStore(token_counter=count_tokens)
    tid = uuid.UUID(str(thread_id))

    # Build the preamble from brain_store's bounded view. load_for_llm
    # summarises on the fly if the thread outgrew the budget. It pulls every
    # finalised row including the user message we just appended, so drop that
    # trailing turn — it's re-added explicitly as the current message.
    history = store.load_for_llm(tid, max_tokens=max_history_tokens)
    if history and history[-1].get("role") == "user" and (history[-1].get("content") or "").strip() == user_msg.strip():
        history = history[:-1]
    preamble = format_history_for_openclaw(history)

    directive = _build_enforce_directive(enforce)
    # Authoritative current date. OpenClaw (v2026.5.28) no longer pins the date
    # inline in the system prompt — it only states the timezone and defers to a
    # session_status tool the model doesn't reliably call — so qwen hallucinates
    # the weekday (said "Saturday" for a Wednesday). Pin it right before the user
    # message, mirroring tg_bot/scheduler.py. Host clock is Asia/Singapore.
    date_stamp = (
        f"[Today is {datetime.now():%A, %d %B %Y} (Asia/Singapore). Use this as "
        "the authoritative current date; do not compute the day of week yourself.]"
    )
    pieces = [p for p in (preamble, directive, date_stamp, user_msg) if p]
    openclaw_input = "\n\n".join(pieces)

    # Fresh per-turn session-id — OpenClaw carries no state between our turns;
    # brain_store is the sole continuity layer. The turnstile keeps this
    # embedded invocation from racing the persistent gateway / another turn in
    # ANY process (web bridge + Telegram bot are separate processes).
    fresh_session = str(uuid.uuid4())
    logger.info(
        "chat_turn thread=%s history_msgs=%d preamble_chars=%d openclaw_session=%s",
        tid, len(history), len(preamble), fresh_session[:8],
    )
    _t_acquire = time.time()
    try:
        with gateway_turnstile():
            _waited_ms = int((time.time() - _t_acquire) * 1000)
            if _waited_ms > 500:
                logger.info("turnstile contended: waited %dms before this turn (session=%s)", _waited_ms, fresh_session[:8])
            turn = openclaw_one_shot(
                session_id=fresh_session, message=openclaw_input, timeout_s=timeout_s
            )
        reply = extract_reply(turn)
    except Exception as exc:
        # Turn-level fence (A-resolution 3.3 / P2+P3): a hard failure — the
        # subprocess kill-fence (HARD_TIMEOUT_S) raising TimeoutExpired, a WSL
        # crash, a dead gateway — must NOT propagate and leave the reserved
        # assistant row in-flight forever. That orphans the row AND (under
        # `-np 1`) keeps the single slot looking busy. Synthesize a failed reply
        # so the finalize below force-closes the row, instead of trusting
        # OpenClaw's (unwired, #71127) self-abort or the no-op watchdog restart.
        logger.error(
            "turn EXC session=%s %s: %s",
            fresh_session[:8], type(exc).__name__, str(exc)[:300],
        )
        reply = {
            "ok": False,
            "error": "turn_exception",
            "detail": f"{type(exc).__name__}: {str(exc)[:280]}",
        }

    if not reply.get("ok"):
        logger.error("turn NOT ok session=%s error=%r detail=%r", fresh_session[:8], reply.get("error"), (reply.get("detail") or "")[:300])

    # A-resolution 3.2 — stop the error-WRITE poison. The shipped read-side
    # filter only excludes rows whose CONTENT matches a sentinel denylist; a
    # failed turn that came back with PARTIAL reply text would otherwise be
    # persisted as clean, replayable assistant content the denylist can't catch.
    # Gate on `reply.ok`, NOT on content: on any non-ok turn always persist the
    # canonical [bridge_error] sentinel so the loader filter excludes it. A short
    # partial-text tail is kept inside the sentinel for forensics — it still
    # starts with [bridge_error], so it stays non-replayable.
    if reply.get("ok"):
        final_text = reply.get("reply") or ""
    else:
        _partial = (reply.get("reply") or "").strip()
        _detail = reply.get("detail") or reply.get("error") or "unknown"
        final_text = f"[bridge_error] {_detail}"
        if _partial:
            final_text += f" | partial[{len(_partial)}]: {_partial[:200]}"

    finalized: Message = store.finalize(
        message_id=assistant_message_id,
        content=final_text,
        tokens_in=reply.get("prompt_tokens"),
        tokens_out=reply.get("completion_tokens") or count_tokens(final_text),
        model=reply.get("model"),
    )

    return {
        "ok": reply.get("ok", False),
        "thread_id": str(tid),
        "assistant_message_id": finalized.id,
        "reply": final_text,
        "model": reply.get("model"),
        "provider": reply.get("provider"),
        "duration_ms": reply.get("duration_ms"),
        "prompt_tokens": reply.get("prompt_tokens"),
        "completion_tokens": reply.get("completion_tokens"),
        "context_limit": reply.get("context_limit"),
        "media": reply.get("media") or [],
        "error": reply.get("error"),
        "error_detail": reply.get("detail"),
    }


def chat_turn(
    thread_id: uuid.UUID | str,
    user_msg: str,
    surface: str,
    store: BrainStore | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    enforce: dict | None = None,
    max_history_tokens: int = 6000,
) -> dict:
    """Run one chat turn end-to-end, synchronously (stateless OpenClaw — Phase
    7.5). Thin wrapper over `chat_turn_begin` + `chat_turn_finish` kept for
    surfaces that genuinely want to block (e.g. the TG bot). The Mini App
    instead calls begin/finish separately so its POST returns immediately.

    `enforce`: optional `{"kind": "mcp"|"skill", "name": "<service>"}` —
    prepends a `[Use the X service]` directive to OpenClaw's input only
    (not persisted).
    """
    store = store or BrainStore(token_counter=count_tokens)
    begun = chat_turn_begin(thread_id, user_msg, surface, store=store)
    result = chat_turn_finish(
        thread_id, user_msg, begun["assistant_message_id"],
        store=store, timeout_s=timeout_s, enforce=enforce,
        max_history_tokens=max_history_tokens,
    )
    result["user_message_id"] = begun["user_message_id"]
    return result
