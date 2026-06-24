"""Stream monitor (V1) — polls a watchlist of channel/streamer URLs and
DMs the user when one goes live.

Design contract:
- One watchlist per bot (owner-only). V1 doesn't support multi-user lists.
- Poll cadence is conservative (5 min default). Each probe is yt-dlp
  extract_info(download=False) — costs ~1-3s and a small HTTP request,
  no scraping HTML directly.
- State is OFFLINE / LIVE per entry. On OFFLINE → LIVE transition, send
  a Telegram DM with inline keyboard "Yes — record" / "No — skip".
- LIVE → OFFLINE just resets state silently (no "stream ended" spam).
- Watchlist file at /data/watchlist.json — JSON list of {url, label,
  added_by, added_at}. Survives container restart. Hand-editable.
- Probes that error out (timeout, rate-limit, network) are logged but
  treated as 'still offline'. We don't notify on errors — too noisy.

V2 ideas (not built):
- Multi-user lists (per-chat-id watchlists)
- Persistent state across restart (currently re-detects "live" on restart
  and re-prompts — V2 could remember the prompt was already sent)
- Adaptive poll cadence (faster when streamer is "usually live around now")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from .config import (
    MONITOR_ENABLED,
    MONITOR_POLL_INTERVAL_SECONDS,
    MONITOR_PROBE_TIMEOUT_SECONDS,
    OWNER_CHAT_ID,
)
from .downloader import _resolve_cookies
from .i18n import get_lang, t

logger = logging.getLogger(__name__)

WATCHLIST_FILE = Path(os.environ.get("WATCHLIST_FILE", "/data/watchlist.json"))


def _load_watchlist() -> list[dict[str, Any]]:
    if not WATCHLIST_FILE.exists():
        return []
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("Failed to read watchlist %s: %s", WATCHLIST_FILE, e)
        return []


def _save_watchlist(entries: list[dict[str, Any]]) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHLIST_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    tmp.replace(WATCHLIST_FILE)


# Hostname-substring → human-readable platform label. Matched in order, so
# longer/more-specific keys should come first. Falls back to title-cased
# second-level domain ("foo.bar.com" → "Bar").
_PLATFORM_MAP: list[tuple[str, str]] = [
    ("chaturbate.com",   "Chaturbate"),
    ("stripchat.com",    "Stripchat"),
    ("bongacams.com",    "BongaCams"),
    ("cam4.com",         "Cam4"),
    ("twitch.tv",        "Twitch"),
    ("kick.com",         "Kick"),
    ("youtube.com",      "YouTube"),
    ("youtu.be",         "YouTube"),
    ("instagram.com",    "Instagram"),
    ("tiktok.com",       "TikTok"),
    ("twitter.com",      "Twitter/X"),
    ("x.com",            "Twitter/X"),
    ("facebook.com",     "Facebook"),
    ("fb.watch",         "Facebook"),
    ("reddit.com",       "Reddit"),
    ("vimeo.com",        "Vimeo"),
    ("rumble.com",       "Rumble"),
    ("dlive.tv",         "DLive"),
    ("trovo.live",       "Trovo"),
    ("bilibili.com",     "Bilibili"),
    ("douyu.com",        "Douyu"),
]


def extract_platform(url: str) -> str:
    """Return a display platform name (e.g. 'Twitch', 'Chaturbate') from URL.
    Falls back to a title-cased second-level domain so unknown sites still
    group sensibly in the Mini App."""
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.hostname or "").lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        for needle, label in _PLATFORM_MAP:
            if needle in host:
                return label
        parts = host.split(".")
        if len(parts) >= 2:
            return parts[-2].capitalize()
        return host or "Other"
    except Exception:
        return "Other"


def extract_username(url: str) -> str:
    """Pull a display username out of a streamer URL.

    Examples:
      chaturbate.com/dewdropdoll/   → dewdropdoll
      twitch.tv/somechannel         → somechannel
      kick.com/streamer             → streamer
      youtube.com/@handle           → handle
      youtube.com/c/channel         → channel
      Anything weird                → host/path-suffix fallback (never empty)
    """
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.hostname or "").lower().lstrip("www.")
        path = (p.path or "").strip("/")
        if not path:
            return host or url
        parts = [seg for seg in path.split("/") if seg]
        # youtube.com/@handle  → handle
        if parts and parts[0].startswith("@"):
            return parts[0][1:]
        # youtube.com/c/channel | youtube.com/user/x | youtube.com/channel/UCxxxx
        if parts[0] in ("c", "user", "channel") and len(parts) > 1:
            return parts[1]
        # tiktok.com/@user
        if "tiktok" in host and parts[0].startswith("@"):
            return parts[0][1:]
        # twitch / kick / chaturbate / stripchat / cam4 / generic → first path segment
        return parts[0]
    except Exception:
        return url


def _normalize_url(url: str) -> str:
    """Defensive scheme injection — bare 'twitch.tv/foo' becomes
    'https://twitch.tv/foo' so Mini App tap-to-open doesn't resolve it
    against media.your-domain.example.com and 404."""
    u = (url or "").strip()
    if u and not re.match(r"^https?://", u, re.IGNORECASE):
        u = "https://" + u.lstrip("/")
    return u


def add_to_watchlist(url: str, label: str | None = None, added_by: int | None = None) -> tuple[bool, str]:
    """Returns (added, message). Idempotent — duplicate URL returns (False, ...)."""
    url = _normalize_url(url)
    entries = _load_watchlist()
    if any(e.get("url") == url for e in entries):
        return False, f"Already watching {url}"
    entries.append({
        "url":      url,
        "label":    label or url,
        "added_by": added_by,
        "added_at": int(time.time()),
    })
    _save_watchlist(entries)
    return True, f"Now watching {url}"


def remove_from_watchlist(url: str, chat_id: int | None = None) -> tuple[bool, str]:
    """Remove a watchlist entry. If `chat_id` is given, only removes when the
    entry's `added_by` matches (so user A can't delete user B's entries from
    the Mini App). Owner uses chat_id=None to bypass."""
    entries = _load_watchlist()
    def _kept(e):
        if e.get("url") != url: return True
        if chat_id is not None and e.get("added_by") != chat_id: return True
        return False
    new = [e for e in entries if _kept(e)]
    if len(new) == len(entries):
        return False, f"Not in watchlist: {url}"
    _save_watchlist(new)
    return True, f"Removed {url}"


def list_watchlist(chat_id: int | None = None) -> list[dict[str, Any]]:
    """Return watchlist entries. If `chat_id` given, filters to entries this
    user added. Owner uses chat_id=None to see all."""
    entries = _load_watchlist()
    if chat_id is None:
        return entries
    return [e for e in entries if e.get("added_by") == chat_id]


def update_watchlist_entry(old_url: str, new_url: str | None = None,
                            label: str | None = None,
                            chat_id: int | None = None) -> tuple[bool, str]:
    """Edit a watchlist entry in place. If `chat_id` is given, only updates
    when the entry's `added_by` matches. Owner uses chat_id=None to bypass.

    If `new_url` is given and differs from old_url, also remaps the in-memory
    status entry so the green/red dot doesn't reset to 'unknown'."""
    entries = _load_watchlist()
    target = None
    for e in entries:
        if e.get("url") == old_url:
            if chat_id is not None and e.get("added_by") != chat_id:
                return False, "Not your entry"
            target = e
            break
    if target is None:
        return False, f"Not in watchlist: {old_url}"
    # Reject duplicate URL collisions
    if new_url:
        new_url = _normalize_url(new_url)
    if new_url and new_url != old_url:
        if any(e.get("url") == new_url for e in entries):
            return False, f"Already watching {new_url}"
        target["url"] = new_url
        # Migrate status cache so the dot survives the rename.
        if old_url in _last_status:
            _last_status[new_url] = _last_status.pop(old_url)
    if label is not None:
        target["label"] = label or target.get("url")
    _save_watchlist(entries)
    return True, "Updated"


def set_muted(url: str, muted: bool, chat_id: int | None = None) -> tuple[bool, str]:
    """Flip the `muted` flag on a watchlist entry. Muted entries are still
    polled (so the green/red dot stays current) but no LIVE prompt is sent."""
    entries = _load_watchlist()
    for e in entries:
        if e.get("url") == url:
            if chat_id is not None and e.get("added_by") != chat_id:
                return False, "Not your entry"
            e["muted"] = bool(muted)
            _save_watchlist(entries)
            return True, "Muted" if muted else "Unmuted"
    return False, f"Not in watchlist: {url}"


def get_status(url: str) -> str:
    """Return last-seen status for a URL: 'live' | 'offline' | 'unknown'."""
    return _last_status.get(url, "unknown")


def get_status_map() -> dict[str, str]:
    """Snapshot of all known URL → status mappings (live/offline). Used by the
    Mini App to colour the row dot without making N HTTP probes."""
    return dict(_last_status)


def snooze_streamer(url: str, minutes: int) -> int:
    """Set snoozed_until on a watchlist entry to now + minutes. Returns the
    epoch seconds at which the snooze expires (0 if URL not in watchlist)."""
    entries = _load_watchlist()
    expires_at = int(time.time() + minutes * 60)
    found = False
    for e in entries:
        if e.get("url") == url:
            e["snoozed_until"] = expires_at
            found = True
            break
    if found:
        _save_watchlist(entries)
        return expires_at
    return 0


def is_snoozed(entry: dict[str, Any]) -> bool:
    snoozed_until = int(entry.get("snoozed_until") or 0)
    return snoozed_until > time.time()


def _probe_is_live(url: str) -> dict[str, Any]:
    """Synchronous yt-dlp probe. Returns {is_live, title, uploader, error}.

    Errors are NOT raised — they're returned in the dict so the caller can
    decide policy (typically: log + treat as offline).
    """
    cookiepath = _resolve_cookies(url)
    opts: dict = {"quiet": True, "no_warnings": True, "socket_timeout": MONITOR_PROBE_TIMEOUT_SECONDS}
    if cookiepath:
        opts["cookiefile"] = cookiepath
    # Cloudflare-protected sites need Chrome TLS impersonation (HTTP 406 otherwise).
    from .live_downloader import _add_impersonate_if_needed
    _add_impersonate_if_needed(opts, url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return {"is_live": False, "error": str(e)[:200]}
    except Exception as e:
        return {"is_live": False, "error": str(e)[:200]}
    if not info:
        return {"is_live": False, "error": "no info"}
    is_live = bool(info.get("is_live")) or (info.get("live_status") or "").lower() in ("is_live",)
    return {
        "is_live":  is_live,
        "title":    info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "error":    None,
    }


# In-memory state of last-seen status per URL. Keys are URLs; values are
# 'live' or 'offline'. Resets on container restart (intentional V1 trade-off).
_last_status: dict[str, str] = {}


async def _poll_once(app: Application, entries: list[dict[str, Any]]) -> None:
    """Probe every watchlist entry once, dispatch transitions to OWNER_CHAT_ID."""
    if not entries:
        return
    if OWNER_CHAT_ID is None:
        logger.warning("monitor: OWNER_CHAT_ID not set — skipping prompts")
        return

    loop = asyncio.get_running_loop()
    for entry in entries:
        url = entry.get("url")
        label = entry.get("label") or url
        if not url:
            continue
        try:
            result = await loop.run_in_executor(None, _probe_is_live, url)
        except Exception as e:
            logger.warning("monitor: probe %s crashed: %s", url, e)
            continue

        if result.get("error"):
            logger.debug("monitor: %s probe error (treating as offline): %s", label, result["error"])
            _last_status[url] = "offline"
            continue

        is_live = result["is_live"]
        prev = _last_status.get(url)
        new = "live" if is_live else "offline"
        _last_status[url] = new

        # Mute check: muted entries are still probed (so the Mini App dot
        # stays current) but never trigger a Telegram prompt. Mute is
        # indefinite; snooze is time-bound.
        if entry.get("muted"):
            if prev != "live" and is_live:
                logger.info("monitor: %s went LIVE but is muted — skipping prompt", label)
            continue

        # Snooze check: if user explicitly snoozed this streamer, skip the
        # prompt regardless of state transition. We still update _last_status
        # above so that when snooze expires we don't immediately re-prompt
        # for a streamer who's been live the whole time.
        if is_snoozed(entry):
            if prev != "live" and is_live:
                until = int(entry.get("snoozed_until") or 0)
                logger.info(
                    "monitor: %s went LIVE but is snoozed until %s — skipping prompt",
                    label, until,
                )
            continue

        if prev != "live" and is_live:
            # OFFLINE → LIVE transition. Notify owner with inline keyboard.
            # Prefer the URL-extracted username (stable: 'dewdropdoll') over
            # yt-dlp's `uploader` field (sometimes blank or human-name).
            uname = extract_username(url) or (result.get("uploader") or label)
            platform = extract_platform(url)
            owner_lang = get_lang(OWNER_CHAT_ID)
            text = t(
                "monitor_live_prompt", owner_lang,
                platform=platform, uploader=uname,
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(t("btn_yes_record", owner_lang), callback_data=f"mon:rec:{url}"),
                    InlineKeyboardButton(t("btn_skip", owner_lang),       callback_data=f"mon:skip:{url}"),
                ],
                [
                    InlineKeyboardButton(t("btn_snooze_1h", owner_lang), callback_data=f"mon:snooze1h:{url}"),
                    InlineKeyboardButton(t("btn_snooze_8h", owner_lang), callback_data=f"mon:snooze8h:{url}"),
                ],
            ])
            try:
                await app.bot.send_message(
                    chat_id=OWNER_CHAT_ID,
                    text=text,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                logger.info("monitor: %s went LIVE — prompt sent", label)
            except Exception as e:
                logger.error("monitor: failed to send live notification for %s: %s", label, e)
        elif prev == "live" and not is_live:
            logger.info("monitor: %s went OFFLINE", label)


async def monitor_loop(app: Application) -> None:
    """Forever loop. Sleeps between polls. Cancellable."""
    if not MONITOR_ENABLED:
        logger.info("monitor: disabled in config")
        return
    logger.info(
        "monitor: started (interval=%ds, watchlist=%s)",
        MONITOR_POLL_INTERVAL_SECONDS, WATCHLIST_FILE,
    )
    try:
        while True:
            entries = _load_watchlist()
            if entries:
                logger.debug("monitor: polling %d entries", len(entries))
                await _poll_once(app, entries)
            await asyncio.sleep(MONITOR_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("monitor: cancelled")
        raise
    except Exception as e:
        logger.exception("monitor: loop crashed: %s", e)
        raise
