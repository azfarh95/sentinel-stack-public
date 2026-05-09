"""Telegram bot — detects video URLs, downloads, sends back. No AI involved."""

import asyncio
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .config import ALLOWED_CHAT_IDS, DELETE_AFTER_SEND, OWNER_CHAT_ID
from .downloader import download, identify_post, send_files
from .interceptor import find_video_url

logger = logging.getLogger(__name__)

SMDL_BOT_TOKEN = os.environ["SMDL_BOT_TOKEN"]

_app: Application | None = None


def get_application() -> Application:
    return _app


async def build() -> Application:
    global _app
    _app = Application.builder().token(SMDL_BOT_TOKEN).build()

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        logger.info("Received update: chat=%s text=%r",
                    msg.chat_id if msg else None,
                    (msg.text or "")[:80] if msg else None)

        if not msg or not msg.text:
            return

        chat_id = msg.chat_id
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            logger.info("Ignoring chat_id %s (not in ALLOWED_CHAT_IDS)", chat_id)
            return

        result = find_video_url(msg.text)
        if not result:
            logger.info("No video URL found in: %r", msg.text[:80])
            return

        platform, url = result
        logger.info("Video URL detected [%s]: %s", platform, url[:80])

        status_msg = await msg.reply_text(f"Identifying {platform} post...")

        try:
            info = await identify_post(url)
        except Exception as e:
            await status_msg.edit_text(f"Failed to identify post: {e}")
            return

        if info.get("error"):
            is_private = info.get("is_private", False)
            err_text = "Private account — cannot download." if is_private else f"Could not identify post: {info['error'][:200]}"
            await status_msg.edit_text(err_text)
            return

        media_type = info.get("media_type", "video")
        count = info.get("count", 1)
        uploader = info.get("uploader") or info.get("uploader_id") or platform
        media_label = {
            "photo":    "photo",
            "carousel": f"carousel ({count} items)",
            "video":    "video",
        }.get(media_type, "media")

        is_owner = (OWNER_CHAT_ID is not None and chat_id == OWNER_CHAT_ID)

        try:
            await status_msg.edit_text(
                f"{platform} · @{uploader} · {media_label}\nDownloading..."
            )

            result = await download(url, media_type=media_type, is_owner=is_owner)

            if result.get("error"):
                await status_msg.edit_text(f"Download failed: {result['error']}")
                return

            files = result["files"]
            cached = result.get("cached", False)
            file_count = len(files)
            title = info.get("title", "")

            await status_msg.edit_text(
                f"{'Cached · s' if cached else 'S'}ending {file_count} files..."
                if file_count > 1 else
                f"{'Cached · s' if cached else 'S'}ending {platform} {media_label}..."
            )

            send_result = await send_files(ctx.bot, chat_id, files, caption=title)

            if send_result.get("ok"):
                sent = send_result.get("count", 1)
                size = send_result.get("size_mb")
                cached_tag = " · cached" if cached else ""
                detail = f"{sent} file{'s' if sent > 1 else ''}" + (f" · {size} MB" if size else "") + cached_tag
                await status_msg.edit_text(f"Sent ({detail})")

                if DELETE_AFTER_SEND and not cached:
                    for fp in files:
                        try:
                            Path(fp).unlink()
                        except Exception:
                            pass
            elif send_result.get("error") == "file_too_large":
                await status_msg.edit_text(
                    f"File too large for Telegram ({send_result['size_mb']} MB). "
                    f"Saved locally at {files[0]}"
                )
            else:
                await status_msg.edit_text(f"Send failed: {send_result.get('error')}")

        except Exception as e:
            logger.exception("Download pipeline error")
            await status_msg.edit_text(f"Error: {e}")

    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return _app
