"""Telethon user-account uploader for files exceeding the bot API's 50 MB cap.

Used as a fallback after a successful download (live or regular) when the
file is too big to send through the bot API. The user account has a 2 GB
per-file upload limit — covers ~95% of realistic recordings.

Sends to the same chat where the bot received the request, so the file
appears alongside the bot's own messages in the user's view.

Credentials come from env vars (set in docker-compose, sourced from WCM):
  TELETHON_API_ID
  TELETHON_API_HASH
  TELETHON_SESSION   (StringSession, generated once via interactive flow)

If any are missing, upload returns {ok: false, error: 'not_configured'}
and the caller falls back to printing the file path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Bot API hard cap for file send
BOT_API_LIMIT_BYTES = 50 * 1024 * 1024

# Telethon user-account cap (Telegram-imposed per-file limit for non-premium)
USER_ACCOUNT_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

_TELETHON_AVAILABLE = True
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:  # noqa: BLE001 — keep smdl importable even if telethon is missing
    _TELETHON_AVAILABLE = False


def _credentials() -> tuple[int, str, str] | None:
    api_id_str = os.environ.get("TELETHON_API_ID", "").strip()
    api_hash   = os.environ.get("TELETHON_API_HASH", "").strip()
    session    = os.environ.get("TELETHON_SESSION", "").strip()
    if not api_id_str or not api_hash or not session:
        return None
    try:
        return (int(api_id_str), api_hash, session)
    except ValueError:
        return None


def is_configured() -> bool:
    """Cheap check the bot can use to decide whether the fallback is available."""
    return _TELETHON_AVAILABLE and _credentials() is not None


async def upload_file(
    filepath: str,
    chat_id: int,
    caption: str | None = None,
    progress_cb: callable | None = None,
) -> dict:
    """Upload a file to a chat via the user account.

    progress_cb (optional, sync): receives (sent_bytes, total_bytes). Telethon
    calls this many times during a multi-MB upload; throttle if you forward
    to Telegram, since edit-message-text is rate limited.

    Returns:
        {"ok": True,  "message_id": int, "size_mb": float}
        {"ok": False, "error": str, "detail": str}
    """
    if not _TELETHON_AVAILABLE:
        return {"ok": False, "error": "telethon_not_installed",
                "detail": "telethon package missing in container"}

    creds = _credentials()
    if not creds:
        return {"ok": False, "error": "not_configured",
                "detail": "TELETHON_API_ID/HASH/SESSION env vars missing"}
    api_id, api_hash, session = creds

    path = Path(filepath)
    if not path.exists():
        return {"ok": False, "error": "file_missing", "detail": str(filepath)}

    size_bytes = path.stat().st_size
    if size_bytes == 0:
        return {"ok": False, "error": "file_empty", "detail": "0-byte file, nothing to upload"}
    if size_bytes > USER_ACCOUNT_LIMIT_BYTES:
        return {"ok": False, "error": "exceeds_user_account_limit",
                "detail": f"{size_bytes / 1024 / 1024 / 1024:.2f} GB > 2 GB cap"}

    size_mb = round(size_bytes / 1024 / 1024, 1)
    logger.info("telethon: uploading %s (%s MB) to chat %s", path.name, size_mb, chat_id)

    client = TelegramClient(StringSession(session), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"ok": False, "error": "session_invalid",
                    "detail": "stored TELETHON_SESSION is no longer authorized"}

        # send_file decides video vs document based on extension; we let it.
        # supports_streaming=True hints Telegram clients to play inline for video formats.
        msg = await client.send_file(
            chat_id,
            file=str(path),
            caption=caption,
            supports_streaming=True,
            progress_callback=progress_cb,
        )
        return {"ok": True, "message_id": getattr(msg, "id", None), "size_mb": size_mb}
    except Exception as e:  # noqa: BLE001 — surface any telethon error to caller
        logger.exception("telethon upload failed")
        return {"ok": False, "error": "upload_failed", "detail": str(e)[:300]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
