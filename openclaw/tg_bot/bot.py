"""Telegram long-poll loop for the shared-brain sidecar.

Reads a bot token from WCM (preferred) or env var; loops on
`getUpdates`; hands each update to `TelegramDispatcher.handle_update`;
sends replies via `sendMessage`.

Run:
    py -3 -m openclaw.tg_bot.bot

Env vars:
    SENTINEL_TG_BOT_TOKEN          — explicit override (skips WCM lookup)
    SENTINEL_TG_BOT_TOKEN_WCM_KEY  — WCM key name (default: 'tg_bot_token')
    SENTINEL_TG_BOT_TOKEN_WCM_SVC  — WCM service name (default: 'sentinel-shared-brain')
    SENTINEL_TG_OWNER_ID           — Telegram user ID for ACL (default: 0 = off, anyone can chat)
    SENTINEL_TG_DRY_RUN            — '1' to log replies instead of sending
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
import urllib.parse
import urllib.request
from typing import Iterable

from openclaw.brain_store import BrainStore
from openclaw.brain_wrapper import chat_turn
from openclaw.tokenizer import count_tokens
from openclaw.tg_bot.dispatcher import Outbox, TelegramDispatcher
from openclaw.tg_bot.mirror import start_mirror
from openclaw.tg_bot.topic_sync import start_topic_sync


logger = logging.getLogger("openclaw.tg_bot")

# Periodic orphan-reaper cadence (A-resolution 3.3). 30 min: frequent enough to
# clear a stuck-then-killed turn promptly, far longer than the 900s turn fence
# so it never races a legitimately in-flight turn.
_REAP_INTERVAL_S = 1800


def _load_token() -> str:
    explicit = os.environ.get("SENTINEL_TG_BOT_TOKEN", "").strip()
    if explicit:
        return explicit
    wcm_key = os.environ.get("SENTINEL_TG_BOT_TOKEN_WCM_KEY", "tg_bot_token")
    wcm_svc = os.environ.get("SENTINEL_TG_BOT_TOKEN_WCM_SVC", "sentinel-shared-brain")
    try:
        import keyring
        v = keyring.get_password(wcm_svc, wcm_key)
        if v:
            return v
    except Exception as exc:
        logger.warning("keyring lookup failed: %s", exc)
    raise SystemExit(
        f"No Telegram bot token. Set SENTINEL_TG_BOT_TOKEN or write WCM "
        f"entry service={wcm_svc!r} key={wcm_key!r}."
    )


def _load_owner_id() -> int:
    """Resolve the owner Telegram user-id from env (preferred) or WCM.
    0 = no ACL (anyone can chat); positive int = owner-only."""
    raw = os.environ.get("SENTINEL_TG_OWNER_ID", "").strip()
    if raw:
        return int(raw) if raw.lstrip("-").isdigit() else 0
    try:
        import keyring
        v = keyring.get_password("sentinel-shared-brain", "owner_id")
        if v and v.strip().lstrip("-").isdigit():
            return int(v.strip())
    except Exception as exc:
        logger.warning("owner_id WCM lookup failed: %s", exc)
    return 0


def _load_forum_chat_id() -> int | None:
    """Resolve the forum supergroup chat-id that brain-created threads should
    surface as TG topics in. Env (preferred) → WCM. None = fall back to
    binding-history discovery in topic_sync."""
    raw = os.environ.get("SENTINEL_TG_FORUM_CHAT_ID", "").strip()
    if not raw:
        try:
            import keyring
            raw = (keyring.get_password("sentinel-shared-brain", "forum_chat_id") or "").strip()
        except Exception as exc:
            logger.warning("forum_chat_id WCM lookup failed: %s", exc)
            raw = ""
    if raw and raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _tg_api(token: str, method: str, params: dict, timeout: int = 60) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# The brain bot's REAL slash commands — exactly what dispatcher._handle_command
# routes (start/help/threads/whoami/new + reset/clear aliases). Registered on
# every boot so Telegram's menu can't drift; this replaces the ~70 stale
# OpenClaw *native-gateway* commands the old native Telegram channel left behind
# (this Python bot returns "unknown command" for those — /tools, /model, /gemini…).
_BOT_COMMANDS = [
    {"command": "new",     "description": "Fresh thread — reset context here (also /reset)"},
    {"command": "clear",   "description": "Reset this conversation (alias of /new)"},
    {"command": "compact", "description": "Summarise this thread to shrink its context"},
    {"command": "threads", "description": "List your threads (★ = current topic)"},
    {"command": "rename",  "description": "Rename this thread: /rename <name>"},
    {"command": "model",   "description": "Show the live inference model"},
    {"command": "status",  "description": "Health snapshot — inference + brain store"},
    {"command": "usage",   "description": "Context/token usage for this thread"},
    {"command": "think",   "description": "Toggle step-by-step reasoning mode"},
    {"command": "stop",    "description": "Abort the in-flight turn"},
    {"command": "whoami",  "description": "Show your sender id + access level"},
    {"command": "help",    "description": "Show available commands"},
]


def _set_commands(token: str) -> None:
    """Register the brain bot's command menu, replacing any stale set. The
    default scope is what the ~70 OpenClaw native commands sat in, so setting
    it here overwrites them. Best-effort — never blocks startup."""
    try:
        r = _tg_api(token, "setMyCommands", {"commands": json.dumps(_BOT_COMMANDS)})
        if r.get("ok"):
            logger.info("registered %d bot commands (replaced any stale menu)", len(_BOT_COMMANDS))
        else:
            logger.warning("setMyCommands not ok: %s", r)
    except Exception as e:
        logger.warning("setMyCommands failed: %s", e)


def _tg_download_file(token: str, file_id: str, dest_dir, display_name: str,
                       timeout: int = 60):
    """Download a Telegram attachment to local disk via getFile + the
    https://api.telegram.org/file/bot<TOKEN>/<file_path> URL. Returns
    the Path to the saved file, or None on failure. Caps at the Bot API
    download limit (20 MB) — bigger files fail and we tell the user."""
    import tempfile
    from pathlib import Path as _Path
    try:
        info = _tg_api(token, "getFile", {"file_id": file_id}, timeout=timeout)
        if not info.get("ok"):
            logger.warning("getFile rejected: %s", info)
            return None
        file_path = info["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        req = urllib.request.Request(url)
        dest_dir = _Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize display name + add a short token to avoid clobbering
        import secrets as _sec
        safe_name = "".join(c for c in display_name if c.isalnum() or c in "._- ")[:120] or "attachment"
        dest = dest_dir / f"{_sec.token_urlsafe(8)}__{safe_name}"
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            f.write(r.read())
        return dest
    except Exception as exc:
        logger.warning("file download failed (%s): %s", display_name, exc)
        return None


def _preprocess_attachments(token: str, update: dict, owner_id: int) -> None:
    """In-place rewrite of an incoming TG update so that documents and
    photos become part of the message text — the rest of the dispatcher
    pipeline can then treat them as regular chat input. Mutates update
    so handle_update() sees text with extracted content prepended.

    Silent no-op for updates without document/photo. Bot continues to
    serve text-only messages unchanged."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    doc = msg.get("document")
    photos = msg.get("photo") or []  # array of sizes; pick largest
    if not doc and not photos:
        return

    # Resolve the file_id + a display filename
    if doc:
        file_id = doc.get("file_id")
        display = doc.get("file_name") or "document"
    else:
        # Don't trust array order — TG documents say "newest first" in some
        # places and ascending-size in others, and clients have shipped both.
        # Sort by file_size to actually get the biggest variant. Falls back
        # to width*height when file_size isn't populated.
        def _size_key(p):
            return (p.get("file_size") or
                    (p.get("width", 0) or 0) * (p.get("height", 0) or 0))
        largest = max(photos, key=_size_key)
        file_id = largest.get("file_id")
        ext = ".jpg"
        display = f"photo_{largest.get('file_unique_id', 'image')[:10]}{ext}"
    if not file_id:
        return

    # Save under the same per-user temp tree the bridge uses so /chat
    # and TG attachments share storage + cleanup conventions.
    import tempfile as _tf
    from pathlib import Path as _Path
    dest_dir = _Path(_tf.gettempdir()) / "sentinel-chat-uploads" / str(owner_id)
    saved = _tg_download_file(token, file_id, dest_dir, display)
    if not saved:
        # Couldn't fetch — leave a note in the message so the LLM
        # can apologise meaningfully instead of getting silent text.
        existing = (msg.get("text") or msg.get("caption") or "").strip()
        msg["text"] = (existing + f"\n\n[Attachment {display} arrived but could not be downloaded "
                                  f"(usually >20 MB — Telegram Bot API limit).]").strip()
        return

    # Extract text content from the saved file
    try:
        from openclaw.tg_bot.attachment_processor import extract_from_file
    except Exception as e:
        logger.exception("attachment_processor import failed: %s", e)
        return
    extracted = extract_from_file(saved, display_name=display)

    caption = (msg.get("caption") or "").strip()
    # Build the rewritten user text — keep the caption (if any) AT THE TOP
    # so the LLM sees the user's question first, then the file content.
    if caption:
        rewritten = f"{caption}\n\n{extracted}"
    else:
        rewritten = f"Please look at this file and help me with it.\n\n{extracted}"
    msg["text"] = rewritten


_TG_LIMIT = 4096


def _split_text(text: str, limit: int = 3900) -> list[str]:
    """Split a reply into ≤limit-char chunks for Telegram's 4096 cap, breaking
    on paragraph → line → space boundaries (so HTML tags, which are intra-line,
    aren't cut). A single over-long line is hard-split as a last resort."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def _html_balanced(chunk: str) -> bool:
    """True if the simple inline tags we emit are balanced in this chunk —
    a hard-split could leave an <i>/<b> open, which Telegram would 400 on."""
    for tag in ("i", "b", "code", "pre"):
        if chunk.count(f"<{tag}>") != chunk.count(f"</{tag}>"):
            return False
    return True


def _deliver(token: str, outboxes: Iterable[Outbox], dry_run: bool = False) -> None:
    for box in outboxes:
        if dry_run:
            logger.info("[dry-run] → chat=%s text=%s", box.chat_id, box.text)
            continue
        chunks = _split_text(box.text or "")
        for i, chunk in enumerate(chunks):
            params = {
                "chat_id": str(box.chat_id),
                "text": chunk,
                "disable_web_page_preview": "true",
            }
            # Drop parse_mode on a chunk whose tags got split, else Telegram 400s.
            if box.parse_mode and (len(chunks) == 1 or _html_balanced(chunk)):
                params["parse_mode"] = box.parse_mode
            # Reply-to only the first chunk; thread id on all.
            if box.reply_to_message_id is not None and i == 0:
                params["reply_to_message_id"] = str(box.reply_to_message_id)
                params["allow_sending_without_reply"] = "true"
            if box.message_thread_id is not None:
                params["message_thread_id"] = str(box.message_thread_id)
            try:
                resp = _tg_api(token, "sendMessage", params, timeout=30)
                if not resp.get("ok"):
                    logger.warning("sendMessage rejected (chunk %d/%d): %s", i + 1, len(chunks), resp)
            except Exception as exc:
                logger.error("sendMessage failed (chunk %d/%d): %s", i + 1, len(chunks), exc)


_running = True


def _stop_handler(signum, _frame):
    global _running
    logger.info("signal %s — stopping", signum)
    _running = False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, _stop_handler)
    try:
        signal.signal(signal.SIGTERM, _stop_handler)
    except (AttributeError, ValueError):
        pass

    token = _load_token()
    owner_id = _load_owner_id()
    forum_chat_id = _load_forum_chat_id()
    dry_run = os.environ.get("SENTINEL_TG_DRY_RUN", "0") == "1"

    # Startup getMe — retry a few times so a transient Telegram network blip
    # (WinError 10060 / read timeout) doesn't kill the bot at launch and leave
    # it silently down (the TS task is one-shot, no auto-restart).
    me = {}
    for attempt in range(6):
        try:
            me = _tg_api(token, "getMe", {})
            if me.get("ok"):
                break
            logger.warning("getMe not ok (attempt %d/6): %s", attempt + 1, me)
        except Exception as exc:
            logger.warning("getMe network error (attempt %d/6): %s", attempt + 1, exc)
        time.sleep(min(5 * (attempt + 1), 20))
    if not me.get("ok"):
        logger.error("getMe failed after retries: %s", me)
        return 2
    bot = me["result"]
    logger.info(
        "bot up: @%s id=%s owner_acl=%s dry_run=%s",
        bot.get("username"), bot.get("id"), owner_id or "off", dry_run,
    )
    # Re-assert the command menu every boot so it can't drift back to the stale
    # OpenClaw native-gateway set.
    _set_commands(token)

    store = BrainStore(token_counter=count_tokens)
    # Startup orphan sweep (A-resolution 3.3 / P6): finalize in-flight rows left
    # by a turn whose process was hard-killed (SIGKILL/OOM/reboot) before
    # chat_turn_finish's fence could close them, so they don't read as live
    # "thinking" turns or linger forever (today's baseline: 11, oldest ~20d).
    try:
        reaped = store.reap_orphans()
        if reaped:
            logger.info("startup reaper: finalized %d orphaned in-flight row(s) as [interrupted]", reaped)
    except Exception as exc:
        logger.warning("startup reaper failed (continuing): %s", exc)
    # Chat turns run in a worker thread and deliver via this callback, so the
    # message loop stays free to receive /stop + quick commands mid-turn.
    dispatcher = TelegramDispatcher(
        store=store,
        chat_turn_fn=chat_turn,
        owner_id=owner_id or None,
        tg_token=token,   # for /rename → editForumTopic
        deliver_fn=lambda outs: _deliver(token, outs, dry_run=dry_run),
    )

    # Cross-surface mirror (TG ↔ Mini App / Tauri / CLI).
    # Daemon thread, so it dies cleanly when the main loop exits.
    start_mirror(token=token, store=store, deliver_fn=_deliver, dry_run=dry_run)
    # Forum model: a thread created on web / Android / Comet surfaces here as a
    # new TG forum topic in the configured supergroup, and TG topics map back
    # to threads (see dispatcher _on_topic_* + _handle_chat). forum_chat_id
    # pins where topics are created; without it topic_sync falls back to
    # binding-history discovery.
    start_topic_sync(token=token, store=store, forum_chat_id=forum_chat_id)
    # #42 — /internal/reload-env listener on :8108 (loopback + token-gated).
    # Called by sentinel-watchdog's secrets API after .env.local changes so
    # the brain bot picks up new LLM_API_KEY values without a restart.
    from openclaw.tg_bot._reload_listener import start_reload_listener
    start_reload_listener()
    # In-process recurring-job scheduler. Replaces OpenClaw's native cron for
    # the two announce jobs (MRT status, Wolfies price): the gateway can run
    # the turn but cannot deliver to TG (channel disabled → no outbound
    # adapter; loopback webhook → SSRF-blocked). This delivers via _deliver.
    from openclaw.tg_bot.scheduler import start_scheduler
    start_scheduler(token=token, deliver_fn=_deliver, dry_run=dry_run)

    last_offset = 0
    _last_reap = time.time()
    while _running:
        # Periodic orphan reaper (A-resolution 3.3 / P6) — backstops the
        # chat_turn_finish fence against hard process death that bypasses it.
        # The getUpdates long-poll paces this loop at <=30s, so the gate runs
        # roughly on schedule without a dedicated thread.
        if time.time() - _last_reap > _REAP_INTERVAL_S:
            _last_reap = time.time()
            try:
                reaped = store.reap_orphans()
                if reaped:
                    logger.info("periodic reaper: finalized %d orphan(s) as [interrupted]", reaped)
            except Exception as exc:
                logger.warning("periodic reaper failed (continuing): %s", exc)
        try:
            resp = _tg_api(
                token,
                "getUpdates",
                {
                    "timeout": "30",
                    "offset": str(last_offset),
                    "allowed_updates": json.dumps(["message", "edited_message"]),
                },
                timeout=40,
            )
        except Exception as exc:
            logger.warning("getUpdates errored: %s — backing off 5s", exc)
            time.sleep(5)
            continue

        if not resp.get("ok"):
            logger.warning("getUpdates not ok: %s", resp)
            time.sleep(2)
            continue
        updates = resp.get("result", []) or []
        for upd in updates:
            last_offset = max(last_offset, upd.get("update_id", 0) + 1)
            # Convert document / photo attachments into text content the
            # dispatcher can process. Mutates upd in-place; no-op for
            # plain-text messages.
            try:
                _preprocess_attachments(token, upd, owner_id)
            except Exception as exc:
                logger.exception("attachment preprocess crashed: %s", exc)
            try:
                outboxes = dispatcher.handle_update(upd)
            except Exception as exc:
                logger.exception("dispatcher crashed on update %s", upd.get("update_id"))
                msg = (upd.get("message") or {}).get("chat") or {}
                if msg.get("id") is not None:
                    outboxes = [Outbox(msg["id"], f"⚠ dispatcher error: {exc}")]
                else:
                    outboxes = []
            _deliver(token, outboxes, dry_run=dry_run)

    logger.info("bot stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
