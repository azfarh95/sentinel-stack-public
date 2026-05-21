"""Telegram delivery helpers. Two channels:
  * send()             — production YourSentinelBot to owner DM (user-facing alerts)
  * send_to_testbot()  — claude-assistant-testbot for dev/ops pings (autopilot updates)
"""
import os
import httpx
import logging

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TESTBOT_TOKEN = os.environ.get("TESTBOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")


async def _send_via(token: str, text: str, parse_mode: str | None = "HTML") -> bool:
    if not token:
        logger.warning("token unset — alert dropped: %s", text[:80])
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": OWNER_CHAT_ID, "text": text,
               "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
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


async def send_to_testbot(text: str) -> bool:
    """Send to @Sentinel_claude_testbot_bot (dev/ops pings)."""
    return await _send_via(TESTBOT_TOKEN, text, parse_mode=None)


async def send(text: str) -> bool:
    """Send to @YourSentinelBot (production user-facing alerts)."""
    return await _send_via(TELEGRAM_TOKEN, text, parse_mode="HTML")
