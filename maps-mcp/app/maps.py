"""
Google Maps helpers — URL construction + Telegram inline keyboard delivery.
No API key required. Uses public Google Maps URLs that open in Telegram's WebView.
"""

import logging
import os
from datetime import datetime
from html import escape
from urllib.parse import quote_plus

import httpx
import pytz

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
LOCAL_TZ = os.environ.get("LOCAL_TIMEZONE", "Asia/Kuala_Lumpur")

_MODE_LABEL = {
    "driving":  "🚗 Driving",
    "transit":  "🚇 Transit",
    "walking":  "🚶 Walking",
    "cycling":  "🚴 Cycling",
}

_MODE_PARAM = {
    "driving": "driving",
    "transit": "transit",
    "walking": "walking",
    "cycling": "bicycling",   # Google Maps param name
}


def _normalise_location(loc: str) -> str:
    """
    If loc looks like a bare Singapore postal code (6 digits), append '+Singapore'
    so Google Maps resolves it correctly. Otherwise pass through as-is.
    """
    loc = loc.strip()
    if loc.isdigit() and len(loc) == 6:
        return f"{loc}+Singapore"
    return loc


def _build_directions_url(origin: str, destination: str, mode: str) -> str:
    o = quote_plus(_normalise_location(origin))
    d = quote_plus(_normalise_location(destination))
    travel = _MODE_PARAM.get(mode, "transit")
    # api=1 query-param format resolves postal codes and addresses more reliably than the /dir/A/B/ path format
    return f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}&travelmode={travel}"


def _build_search_url(query: str) -> str:
    q = quote_plus(query.strip())
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def _now_local() -> str:
    tz = pytz.timezone(LOCAL_TZ)
    return datetime.now(tz).strftime("%-I:%M %p")   # e.g. "7:32 PM"


async def _send_telegram(chat_id: str, text: str, button_label: str, button_url: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        log.error("maps: TELEGRAM_BOT_TOKEN not set")
        return False
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": button_label, "url": button_url}
            ]]
        },
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload,
            )
        result = resp.json()
        if not result.get("ok"):
            log.error("maps: Telegram sendMessage failed: %s | chat_id=%s | text=%r", result, chat_id, text)
        return result.get("ok", False)
    except Exception as e:
        log.error("maps: exception sending Telegram message: %s", e)
        return False


# ── Public functions called by MCP tools ──────────────────────────────────────

async def directions(
    chat_id: str,
    origin: str,
    destination: str,
    mode: str = "transit",
) -> dict:
    mode = mode.lower().strip()
    if mode not in _MODE_PARAM:
        mode = "transit"

    maps_url = _build_directions_url(origin, destination, mode)
    mode_label = _MODE_LABEL[mode]
    timestamp = _now_local()

    text = (
        f"🗺️ <b>Directions</b>\n"
        f"<b>From:</b> <code>{escape(origin)}</code>\n"
        f"<b>To:</b> <code>{escape(destination)}</code>\n"
        f"<b>Mode:</b> {mode_label}\n"
        f"<i>Requested at {timestamp}</i>"
    )

    sent = await _send_telegram(
        chat_id, text,
        button_label="Open in Google Maps 🗺️",
        button_url=maps_url,
    )

    if sent:
        return {
            "ok": True,
            "message": f"Directions sent to chat {chat_id}.",
            "maps_url": maps_url,
        }
    return {"error": "Failed to send Telegram message. Check TELEGRAM_BOT_TOKEN."}


async def search(chat_id: str, query: str) -> dict:
    maps_url = _build_search_url(query)
    timestamp = _now_local()

    text = (
        f"🔍 <b>Maps Search</b>\n"
        f"<b>Query:</b> {escape(query)}\n"
        f"<i>Requested at {timestamp}</i>"
    )

    sent = await _send_telegram(
        chat_id, text,
        button_label="Open in Google Maps 🗺️",
        button_url=maps_url,
    )

    if sent:
        return {
            "ok": True,
            "message": f"Map search sent to chat {chat_id}.",
            "maps_url": maps_url,
        }
    return {"error": "Failed to send Telegram message. Check TELEGRAM_BOT_TOKEN."}
