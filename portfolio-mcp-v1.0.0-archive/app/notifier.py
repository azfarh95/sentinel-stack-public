"""Telegram delivery helper. Sends via @YourSentinelBot to the owner's DM."""
import os
import httpx
import logging

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")


async def send(text: str) -> bool:
    if not TELEGRAM_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN unset — alert dropped: %s", text[:80])
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": OWNER_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, data=payload)
            if r.status_code != 200:
                logger.error("Telegram send HTTP %d: %s", r.status_code, r.text[:200])
                return False
            return r.json().get("ok", False)
    except Exception as e:
        logger.exception("Telegram send failed: %s", e)
        return False
