"""Telegram Mini App — owner-only dashboard for SM-DL.

Mounted at /app (HTML) + /api/miniapp/* (JSON).

Features (v1):
  • Recent downloads list (from url_cache)
  • Stream watchlist add/remove (delegates to stream_monitor)
  • Start/stop live recordings (delegates to recorder_bridge.bridge)
  • Supported & configured platforms (from live_downloader registry + config)

Auth: validates Telegram WebApp initData (HMAC-SHA256 with bot token).
Owner-only — initData.user.id must match config.OWNER_CHAT_ID.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

import aiosqlite
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import config as _cfg
from . import database as _db
from . import stream_monitor
from . import auth as _auth
from .database import DB_PATH
from .live_downloader import (
    _PLATFORM_LABELS,    # we read but don't mutate
)
from .recorder_bridge import bridge

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/smdl.json")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")

# config.py exposes UPPER_SNAKE module constants, not a .get() function.
# Some keys also rename between the JSON schema and the module constants.
_KEY_TO_ATTR = {
    "max_concurrent_downloads": "MAX_CONCURRENT",
    # everything else: key.upper() works
}


def _cfg_get(key: str, default=None):
    """Read a config value. Order: JSON file (so UI edits are live) → module
    constant (loaded at import) → caller-provided default."""
    file_cfg = _read_config_file_safe()
    if key in file_cfg:
        return file_cfg[key]
    attr = _KEY_TO_ATTR.get(key, key.upper())
    return getattr(_cfg, attr, default)


def _read_config_file_safe() -> dict:
    """Forward-declared shim so _cfg_get can call into the file reader
    defined later in the module."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            import json as _j
            return _j.load(f)
    except Exception:
        return {}

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Telegram initData validation ─────────────────────────────────────────────


def _validate_init_data(init_data: str, bot_token: str, max_age_s: int = 3600) -> dict:
    """Parse + verify Telegram WebApp initData.

    Returns the parsed payload dict (with 'user' nested as dict) on success.
    Raises HTTPException(401) on failure.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="missing initData")
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=False))
    except Exception:
        raise HTTPException(status_code=401, detail="malformed initData")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="initData hash missing")

    # Reconstruct data-check-string (sorted by key, joined by \n)
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        raise HTTPException(status_code=401, detail="initData signature invalid")

    # Freshness — Telegram's auth_date is unix seconds
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if auth_date and (time.time() - auth_date) > max_age_s:
        raise HTTPException(status_code=401, detail="initData expired")

    # Parse user
    user = {}
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except Exception:
            pass
    pairs["user"] = user
    return pairs


def _is_owner(user_id: int) -> bool:
    owner = _cfg_get("owner_chat_id")
    return owner is not None and int(user_id) == int(owner)


def _allowed_users() -> set[int]:
    """User IDs allowed in the Mini App: owner ∪ ALLOWED_CHAT_IDS."""
    out: set[int] = set()
    owner = _cfg_get("owner_chat_id")
    if owner is not None:
        try: out.add(int(owner))
        except Exception: pass
    for u in (_cfg.ALLOWED_CHAT_IDS or set()):
        try: out.add(int(u))
        except Exception: pass
    return out


async def _check_access(payload: dict) -> int:
    """Returns the caller's user_id if authorised. Routes the decision through
    auth.classify() so the Mini App and the bot agree on who's in/out.
    Raises 403 for banned + unseen users, 503 for admin-only-mode lockout."""
    user_id = (payload.get("user") or {}).get("id")
    if not user_id:
        raise HTTPException(status_code=403, detail="no user in initData")
    decision = await _auth.classify(int(user_id))
    if decision == "allow":
        return int(user_id)
    if decision == "deny_admin_only":
        raise HTTPException(status_code=503,
                            detail="Service is in admin-only mode.")
    if decision == "deny_pending":
        raise HTTPException(status_code=403,
                            detail="Your access is pending owner approval. Send /start to the bot for an approval code.")
    if decision == "deny_unknown":
        raise HTTPException(status_code=403,
                            detail="No bot interaction yet. Send /start to the bot first.")
    # Any banned status (or unexpected denial)
    raise HTTPException(status_code=403,
                        detail="Access denied.")


def _require_owner(payload: dict) -> int:
    """Owner-only guard for settings + onedrive routes."""
    user_id = (payload.get("user") or {}).get("id")
    if not user_id or not _is_owner(int(user_id)):
        raise HTTPException(status_code=403, detail="owner-only")
    return int(user_id)


async def _verify(request: Request) -> dict:
    """Common request guard: HMAC validation + allowed-user check. Owner-only
    routes must call _require_owner(payload) themselves on top of this."""
    # bot.py reads SMDL_BOT_TOKEN — keep this in sync. Fall back to the
    # generic names for cross-deployment portability.
    bot_token = (
        os.environ.get("SMDL_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    )
    if not bot_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    init_data = request.headers.get("x-init-data") or ""
    payload = _validate_init_data(init_data, bot_token)
    await _check_access(payload)
    return payload


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _list_recent_downloads(limit: int = 50) -> list[dict]:
    """Return the most recent N entries from url_cache, newest first."""
    out = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT url, files, platform, uploader, created_at "
            "FROM url_cache ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                d = dict(row)
                try: d["files"] = json.loads(d.get("files") or "[]")
                except Exception: d["files"] = []
                out.append(d)
    return out


def _list_platforms() -> dict:
    """Return supported (from live_downloader) + configured (from config) platforms."""
    configured_live = list(_cfg_get("live_platforms") or [])
    # Registered labels — flatten host_substrings + label
    registered = [
        {"label": label, "hosts": list(hosts)}
        for hosts, label in _PLATFORM_LABELS
    ]
    return {
        "configured_for_live": configured_live,
        "registered_labels": registered,
        # Anything yt-dlp's 1700+ extractors recognise is technically supported,
        # but only those matching a registered label render a friendly name.
        "note": (
            "yt-dlp covers 1700+ sites. The labels list below names the most common; "
            "other URLs route through 'other' but still work if yt-dlp has an extractor."
        ),
    }


def _job_to_dict(job) -> dict:
    return {
        "job_id": job.job_id,
        "chat_id": job.chat_id,
        "url": job.url,
        "platform": job.platform,
        "uploader": job.uploader,
        "started_at": job.started_at,
        "elapsed_sec": int(time.time() - job.started_at),
        "bytes": job.bytes_downloaded,
        "filepath": job.filepath,
        "stop_requested_at": job.stop_requested_at,
        "abort_reason": job.abort_reason,
    }


# ── Request models ───────────────────────────────────────────────────────────


class WatchAddBody(BaseModel):
    url: str
    label: Optional[str] = None


class StreamStartBody(BaseModel):
    url: str


class StreamStopBody(BaseModel):
    chat_id: Optional[int] = None   # default: owner's chat


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/api/miniapp/whoami")
async def whoami(request: Request):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    return {
        "user": p.get("user"),
        "owner_chat_id": _cfg_get("owner_chat_id"),
        "is_owner": _is_owner(uid),
        "allowed_users_count": len(_allowed_users()),
    }


SHARE_SIZE_THRESHOLD = 50 * 1024 * 1024  # 50 MB


def _enrich_with_share_url(row: dict) -> dict:
    """If the row represents a live recording or a large download, attach
    a signed share URL + size, so the Mini App can render a tappable link
    that streams over the public tunnel. Small reels/photos get nothing —
    Telegram already delivered them inline; no actionable Mini App link."""
    from pathlib import Path as _P
    from .file_serve import sign_share_url, DOWNLOADS_DIR as _DLDIR
    files = row.get("files") or []
    if not files:
        return row
    first = files[0]
    try:
        p = _P(first)
        if not p.exists():
            return row
        norm = first.replace("\\", "/")
        is_live = "/live/" in norm
        size = p.stat().st_size
        if is_live or size >= SHARE_SIZE_THRESHOLD:
            try:
                rel = str(p.relative_to(_DLDIR))
            except ValueError:
                # File not under DOWNLOADS_DIR (shouldn't happen but be safe).
                return row
            url = sign_share_url(rel)
            if url:
                row["share_url"] = url
                row["size_mb"]   = round(size / 1024**2, 1)
                row["is_live_recording"] = is_live
    except Exception as _e:
        logger.debug("share_url enrich failed for %s: %s", first, _e)
    return row


@router.get("/api/miniapp/downloads")
async def downloads(request: Request, limit: int = 50):
    """Per-user download history. Owner sees their own attributed downloads
    plus, if the history table is empty for them, falls back to the global
    url_cache (so the tab isn't empty for downloads made before this PR).

    Large downloads + live recordings get a signed share URL attached so the
    Mini App can render a tappable link that streams over the public tunnel."""
    p = await _verify(request)
    uid = int(p["user"]["id"])
    rows = await _db.list_download_history(uid, limit=max(1, min(limit, 200)))
    if not rows and _is_owner(uid):
        rows = await _list_recent_downloads(limit=max(1, min(limit, 200)))
        for r in rows:
            r["downloaded_at"] = r.pop("created_at", None)
            r["source"] = "url_cache (pre-history)"
    rows = [_enrich_with_share_url(r) for r in rows]
    return {"items": rows, "count": len(rows), "user_id": uid}


def _enrich_watchlist_items(items: list[dict]) -> list[dict]:
    """Decorate each watchlist entry with `username`, `platform` (display
    name), and `status` ('live'/'offline'/'unknown', from the monitor's
    in-memory cache). `muted` is read off the entry itself."""
    statuses = stream_monitor.get_status_map()
    snoozed_threshold = time.time()
    out = []
    for e in items:
        url = e.get("url") or ""
        snoozed_until = int(e.get("snoozed_until") or 0)
        out.append({
            **e,
            "username": stream_monitor.extract_username(url),
            "platform": stream_monitor.extract_platform(url),
            "status":   statuses.get(url, "unknown"),
            "muted":    bool(e.get("muted")),
            "snoozed":  snoozed_until > snoozed_threshold,
            "snoozed_until": snoozed_until or None,
        })
    # Sort group-first (platform), then username within group, both case-insensitive.
    out.sort(key=lambda x: ((x.get("platform") or "Other").lower(),
                            (x.get("username") or "").lower()))
    return out


def _active_by_url_for_user(uid: int, is_owner: bool) -> dict[str, dict]:
    """Map url → active-job dict for jobs visible to this user. Owner sees
    every job; non-owner sees only their own."""
    out: dict[str, dict] = {}
    for j in bridge.list_active():
        if not is_owner and j.chat_id != uid:
            continue
        out[j.url] = _job_to_dict(j)
    return out


@router.get("/api/miniapp/watchlist")
async def watchlist(request: Request):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    is_own = _is_owner(uid)
    # Owner sees the global list; everyone else sees only their own entries.
    items = stream_monitor.list_watchlist(chat_id=None if is_own else uid)
    enriched = _enrich_watchlist_items(items)
    # Hide blocked-platform entries from non-owners (they still live in the
    # JSON file — owner can see + edit them, just shielded from regular users).
    if not is_own:
        bl = set(await _auth.get_site_blocklist())
        if bl:
            enriched = [w for w in enriched if w.get("platform") not in bl]
    return {
        "items":   enriched,
        "active":  _active_by_url_for_user(uid, is_own),
        "user_id": uid,
        "scope":   "all" if is_own else "mine",
    }


@router.post("/api/miniapp/watchlist/add")
async def watchlist_add(request: Request, body: WatchAddBody):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    if not _is_owner(uid) and await _auth.is_platform_blocked(body.url):
        return JSONResponse({"ok": False,
                             "error": f"{stream_monitor.extract_platform(body.url)} is disabled by the admin."},
                            status_code=403)
    ok, msg = stream_monitor.add_to_watchlist(body.url, body.label, added_by=uid)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


@router.post("/api/miniapp/watchlist/remove")
async def watchlist_remove(request: Request, body: WatchAddBody):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    # Owner can remove anything; non-owner can only remove their own entries.
    ok, msg = stream_monitor.remove_from_watchlist(body.url,
                                                    chat_id=None if _is_owner(uid) else uid)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


class WatchEditBody(BaseModel):
    url:     str          # current URL (identifier)
    new_url: Optional[str] = None
    label:   Optional[str] = None


class WatchMuteBody(BaseModel):
    url:   str
    muted: bool


@router.post("/api/miniapp/watchlist/edit")
async def watchlist_edit(request: Request, body: WatchEditBody):
    """Edit the URL or label of an existing entry. Used by the Mini App's
    inline edit dropdown — lets the user fix a typo without removing + re-adding."""
    p = await _verify(request)
    uid = int(p["user"]["id"])
    ok, msg = stream_monitor.update_watchlist_entry(
        body.url,
        new_url=(body.new_url.strip() if body.new_url else None) or None,
        label=body.label,
        chat_id=None if _is_owner(uid) else uid,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


@router.post("/api/miniapp/watchlist/mute")
async def watchlist_mute(request: Request, body: WatchMuteBody):
    """Toggle the mute flag. Muted streamers are still polled (so the status
    dot stays current) but won't trigger Telegram LIVE prompts."""
    p = await _verify(request)
    uid = int(p["user"]["id"])
    ok, msg = stream_monitor.set_muted(
        body.url, body.muted,
        chat_id=None if _is_owner(uid) else uid,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


@router.get("/api/miniapp/active")
async def active_streams(request: Request):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    jobs = bridge.list_active()
    # Non-owner sees only their own recording. Owner sees all.
    if not _is_owner(uid):
        jobs = [j for j in jobs if j.chat_id == uid]
    return {"items": [_job_to_dict(j) for j in jobs], "scope": "all" if _is_owner(uid) else "mine"}


@router.post("/api/miniapp/stream/stop")
async def stream_stop(request: Request, body: StreamStopBody):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    target = int(body.chat_id) if body.chat_id else uid
    # Non-owner can only stop their own.
    if not _is_owner(uid) and target != uid:
        raise HTTPException(status_code=403, detail="cannot stop another user's recording")
    status = await bridge.stop(target)
    if status is None:
        return JSONResponse({"ok": False, "error": "no active job for this chat"}, status_code=404)
    return {"ok": True, "chat_id": target, "status": {
        "elapsed_seconds": status.elapsed_seconds,
        "bytes": status.bytes,
        "platform": status.platform,
        "uploader": status.uploader,
    }}


@router.post("/api/miniapp/stream/start")
async def stream_start(request: Request, body: StreamStartBody):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    url = body.url.strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    if not _is_owner(uid) and await _auth.is_platform_blocked(url):
        return JSONResponse({"ok": False,
                             "error": f"{stream_monitor.extract_platform(url)} is disabled by the admin."},
                            status_code=403)
    if bridge.has_job(uid):
        return JSONResponse({"ok": False, "error": "a recording is already active for this user"}, status_code=409)
    asyncio.create_task(bridge.record(chat_id=uid, url=url))
    return {"ok": True, "queued": True, "chat_id": uid, "url": url}


@router.get("/api/miniapp/sites")
async def sites(request: Request):
    """Compact, screenshot-clean view. Strips any adult-category names from
    the response (HIDDEN_FROM_SITES_TAB). Owner-management of those still
    happens in the Admin tab. Also drops non-owner-blocked platforms from
    the visible list."""
    p = await _verify(request)
    uid = int(p["user"]["id"])
    data = _list_platforms()

    # Always-redacted set (adult cam sites).
    redact = set(_auth.HIDDEN_FROM_SITES_TAB)
    # Plus blocklist filter for non-owners.
    if not _is_owner(uid):
        redact |= set(await _auth.get_site_blocklist())
    if redact:
        # registered_labels entries use lowercase label strings; HIDDEN set
        # uses TitleCase. Normalize for comparison.
        redact_lc = {x.lower() for x in redact}
        data["configured_for_live"] = [
            p for p in data.get("configured_for_live", [])
            if p.lower() not in redact_lc
        ]
        data["registered_labels"] = [
            lbl for lbl in data.get("registered_labels", [])
            if (lbl.get("label") or "").lower() not in redact_lc
        ]
    # The verbose yt-dlp note in _list_platforms() isn't shown anymore;
    # the compact UI renders its own one-liner. Return it anyway for any
    # downstream caller that depends on it.
    return data


class TestUrlBody(BaseModel):
    url: str


@router.post("/api/miniapp/test_url")
async def test_url(request: Request, body: TestUrlBody):
    """Classify a pasted URL: platform, live-recording eligibility, and
    whether it's available to the caller. Deliberately silent about adult-
    category platforms — they're treated as 'not available' without naming."""
    p = await _verify(request)
    uid = int(p["user"]["id"])
    url = (body.url or "").strip()
    if not url:
        return {"ok": False, "error": "Paste a URL to test."}

    platform_raw = stream_monitor.extract_platform(url)
    is_owner_user = _is_owner(uid)
    hidden = platform_raw in _auth.HIDDEN_FROM_SITES_TAB
    is_known = platform_raw in {n for _, n in stream_monitor._PLATFORM_MAP}
    live_supported = (
        platform_raw.lower() in {p.lower() for p in (_cfg_get("live_platforms") or [])}
    )

    # Redact platform name in the response if it's adult-category; the
    # availability/recognition info is still accurate (so owner gets the
    # truth, just without the name).
    platform_display = "private category" if hidden else (platform_raw or "other")

    # Availability: owner always passes the gate. Non-owner is subject to
    # the admin blocklist.
    if is_owner_user:
        available = True
        reason: Optional[str] = None
    else:
        blocked = await _auth.is_platform_blocked(url)
        available = not blocked
        reason = ("Not available on this account." if blocked else None)

    if is_known:
        return {
            "ok": True,
            "platform": platform_display,
            "recognised": True,
            "live_supported": live_supported,
            "available": available,
            "reason": reason,
        }

    # Unknown hostname — yt-dlp may still handle it. Don't claim certainty.
    return {
        "ok": True,
        "platform": platform_display,
        "recognised": False,
        "live_supported": False,
        "available": available,
        "reason": (reason or
                   "Unknown site. The bot will try yt-dlp's generic extractor "
                   "— it covers 1700+ sites, but some require cookies or fail."),
    }


# ── Settings / config ────────────────────────────────────────────────────────


# Subset of config keys we expose to the Mini App (numeric/string editable).
# Settings marked needs_restart=True are read once at module import — UI shows
# a "restart required" badge so the user knows.
EDITABLE_SETTINGS = [
    # `admin` flag = surfaces only in the Admin tab (owner-only writes).
    {"key": "max_concurrent_downloads", "label": "Max concurrent downloads",
     "type": "int", "min": 1, "max": 10, "needs_restart": True, "admin": True},
    {"key": "live_max_concurrent", "label": "Max concurrent live recordings",
     "type": "int", "min": 1, "max": 5, "needs_restart": True, "admin": True},
    {"key": "default_quality", "label": "Default download resolution",
     "type": "choice", "choices": ["best", "1080p", "720p", "480p", "360p"]},
    {"key": "live_max_height", "label": "Live recording max height (px, 0=source)",
     "type": "int", "min": 0, "max": 2160, "needs_restart": True, "admin": True},
    {"key": "temp_ttl_hours", "label": "Temp file TTL (hours)",
     "type": "int", "min": 1, "max": 168},
    {"key": "delete_after_send", "label": "Delete files after Telegram send",
     "type": "bool"},
    {"key": "monitor_poll_interval_seconds", "label": "Stream monitor poll interval (s)",
     "type": "int", "min": 60, "max": 3600, "needs_restart": True, "admin": True},
    {"key": "language", "label": "Language",
     "type": "choice", "choices": ["en", "ru"], "needs_restart": True, "admin": True},
    {"key": "timezone", "label": "Timezone (IANA name, e.g. Asia/Singapore)",
     "type": "choice",
     "choices": ["UTC", "Asia/Singapore", "Asia/Kuala_Lumpur", "Asia/Jakarta",
                 "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Dubai", "Asia/Kolkata",
                 "Europe/London", "Europe/Berlin", "Europe/Moscow",
                 "America/New_York", "America/Los_Angeles", "Australia/Sydney"],
     "needs_restart": True, "admin": True},
    {"key": "onedrive_mode", "label": "OneDrive upload mode",
     "type": "choice",
     "choices": ["disabled", "auto_after_send", "on_demand"],
     "admin": True},
    {"key": "onedrive_folder", "label": "OneDrive base folder",
     "type": "string", "admin": True},
    {"key": "onedrive_delete_after_upload",
     "label": "Delete local file after successful OneDrive upload",
     "type": "bool", "admin": True},
]


def _read_config_file() -> dict:
    """Read the smdl.json file. Returns empty dict if missing."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("config read failed: %s", e)
        return {}


def _write_config_file(updates: dict) -> dict:
    """Merge `updates` into smdl.json and write atomically. Returns the merged config."""
    try:
        from pathlib import Path
        cfg_path = Path(CONFIG_FILE)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        current = _read_config_file()
        current.update(updates)
        tmp = cfg_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, sort_keys=True)
        tmp.replace(cfg_path)
        return current
    except Exception as e:
        logger.error("config write failed: %s", e)
        raise HTTPException(status_code=500, detail=f"config write failed: {e}")


def _disk_usage_gb(path: str) -> dict:
    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        return {"total_gb": round(total / 1024**3, 1),
                "used_gb":  round(used  / 1024**3, 1),
                "free_gb":  round(free  / 1024**3, 1)}
    except Exception:
        return {"total_gb": None, "used_gb": None, "free_gb": None}


@router.get("/api/miniapp/config")
async def get_config(request: Request):
    await _verify(request)
    # Current values come from the loaded _cfg module (single source of truth).
    current_values = {}
    for s in EDITABLE_SETTINGS:
        current_values[s["key"]] = _cfg_get(s["key"])
    return {
        "settings": EDITABLE_SETTINGS,
        "values": current_values,
        "paths": {
            "downloads_dir": DOWNLOADS_DIR,
            "downloads_dir_writable": os.access(DOWNLOADS_DIR, os.W_OK) if os.path.exists(DOWNLOADS_DIR) else False,
            "config_file": CONFIG_FILE,
        },
        "disk": _disk_usage_gb(DOWNLOADS_DIR),
    }


class ConfigUpdateBody(BaseModel):
    updates: dict


@router.post("/api/miniapp/config")
async def update_config(request: Request, body: ConfigUpdateBody):
    p = await _verify(request)
    _require_owner(p)
    # Validate each update against EDITABLE_SETTINGS
    schema = {s["key"]: s for s in EDITABLE_SETTINGS}
    validated = {}
    errors = []
    needs_restart = []
    for k, v in (body.updates or {}).items():
        if k not in schema:
            errors.append(f"{k}: not editable")
            continue
        s = schema[k]
        try:
            if s["type"] == "int":
                vv = int(v)
                if vv < s.get("min", -10**9) or vv > s.get("max", 10**9):
                    raise ValueError(f"out of range [{s.get('min')}, {s.get('max')}]")
                validated[k] = vv
            elif s["type"] == "bool":
                validated[k] = bool(v)
            elif s["type"] == "choice":
                if v not in s["choices"]:
                    raise ValueError(f"not in {s['choices']}")
                validated[k] = v
            else:
                validated[k] = v
        except Exception as e:
            errors.append(f"{k}: {e}")
            continue
        if s.get("needs_restart"):
            needs_restart.append(k)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    merged = _write_config_file(validated)
    return {"ok": True, "saved": validated, "needs_restart": needs_restart,
            "merged_config": merged}


# OneDrive — placeholder for Phase 2. Returns status only.
@router.get("/api/miniapp/onedrive/status")
async def onedrive_status(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import onedrive as _od
    return await _od.get_status()


@router.post("/api/miniapp/onedrive/connect")
async def onedrive_connect(request: Request):
    """Owner-only. Kicks off the MSAL device-code flow. Returns the user_code
    and verification URL — the UI shows them and polls /status until
    `configured` flips true."""
    p = await _verify(request)
    _require_owner(p)
    from . import onedrive as _od
    try:
        return {"ok": True, **(await _od.start_device_flow())}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/miniapp/onedrive/disconnect")
async def onedrive_disconnect(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import onedrive as _od
    removed = _od.disconnect()
    return {"ok": True, "removed": removed}


@router.post("/api/miniapp/onedrive/test_upload")
async def onedrive_test(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import onedrive as _od
    try:
        result = await _od.test_upload()
        return {"ok": True, "name": result.get("name"),
                "webUrl": result.get("webUrl"), "size": result.get("size")}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


class OneDriveUploadBody(BaseModel):
    url:      str        # the original download URL (key for the history lookup)


@router.post("/api/miniapp/onedrive/upload")
async def onedrive_upload(request: Request, body: OneDriveUploadBody):
    """On-demand upload: any allowed user can push one of THEIR OWN history
    rows to OneDrive. The token is owner-scoped (owner's OneDrive), so non-
    owners are effectively contributing into the owner's drive — by design.

    Returns counts; runs synchronously so the toast tells the user what
    happened. For huge multi-file batches that'd block, the bg auto-mirror
    path (auto_after_send) is the right tool."""
    p = await _verify(request)
    uid = int(p["user"]["id"])
    is_own = _is_owner(uid)

    # Look up the history row by (chat_id, url) so users can only push files
    # they actually downloaded.
    rows = await _db.list_download_history(uid, limit=200)
    target = None
    for r in rows:
        if r.get("url") == body.url:
            target = r; break
    if target is None and is_own:
        # Owner fallback: check url_cache for pre-history rows they own.
        cached = await _list_recent_downloads(limit=200)
        for r in cached:
            if r.get("url") == body.url:
                target = r; break
    if target is None:
        return JSONResponse({"ok": False,
                             "error": "Download not found in your history."},
                            status_code=404)

    from . import onedrive as _od
    folder       = _cfg_get("onedrive_folder") or "/SMDL"
    delete_after = bool(_cfg_get("onedrive_delete_after_upload"))
    try:
        summary = await _od.auto_upload_files(
            target.get("files") or [],
            target.get("platform"),
            target.get("uploader"),
            base_folder=folder,
            delete_after_upload=delete_after,
        )
        return {"ok": True, **summary}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Admin: user management + admin-only-mode + site blocklist ────────────────


class UserStatusBody(BaseModel):
    chat_id: int
    reason:  Optional[str] = None


class AdminModeBody(BaseModel):
    enabled: bool
    reason:  Optional[str] = None


class SiteBlocklistBody(BaseModel):
    blocked: list[str]


@router.get("/api/miniapp/admin/users")
async def admin_list_users(request: Request):
    p = await _verify(request)
    _require_owner(p)
    rows = await _db.list_users()
    owner_id = _cfg_get("owner_chat_id")
    for r in rows:
        r["is_owner"] = (owner_id is not None and int(r.get("chat_id") or 0) == int(owner_id))
    return {"items": rows, "count": len(rows)}


@router.post("/api/miniapp/admin/users/ban")
async def admin_ban_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    _require_owner(p)
    if _auth.is_owner(body.chat_id):
        return JSONResponse({"ok": False, "error": "Cannot ban the owner."}, status_code=400)
    ok = await _db.set_user_status(body.chat_id, "banned", body.reason)
    if not ok:
        return JSONResponse({"ok": False, "error": "No such user."}, status_code=404)
    return {"ok": True}


@router.post("/api/miniapp/admin/users/unban")
async def admin_unban_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    _require_owner(p)
    ok = await _db.set_user_status(body.chat_id, "active")
    if not ok:
        return JSONResponse({"ok": False, "error": "No such user."}, status_code=404)
    return {"ok": True}


class ApproveByCodeBody(BaseModel):
    code: str


@router.post("/api/miniapp/admin/users/approve")
async def admin_approve_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    _require_owner(p)
    ok = await _db.approve_user(body.chat_id)
    if not ok:
        return JSONResponse({"ok": False,
                             "error": "User not found, or is banned (unban first)."},
                            status_code=400)
    return {"ok": True}


@router.post("/api/miniapp/admin/users/approve_by_code")
async def admin_approve_by_code(request: Request, body: ApproveByCodeBody):
    """Owner pastes the 9-digit code a pending user sent them out-of-band.
    We look up the matching pending row and promote it to 'active'.
    Fail-closed: bad/expired/already-used codes return 404 with a generic
    error message — no oracle for code-guessing attackers."""
    p = await _verify(request)
    _require_owner(p)
    row = await _db.find_user_by_pending_code(body.code or "")
    if row is None:
        return JSONResponse({"ok": False,
                             "error": "Code not recognised, expired, or already used."},
                            status_code=404)
    await _db.approve_user(int(row["chat_id"]))
    return {
        "ok": True,
        "chat_id": int(row["chat_id"]),
        "username": row.get("username"),
        "first_name": row.get("first_name"),
    }


# ── Admin: approved groups ───────────────────────────────────────────────────


class GroupApproveBody(BaseModel):
    chat_id: int
    label:   Optional[str] = None


class GroupUnapproveBody(BaseModel):
    chat_id: int


@router.get("/api/miniapp/admin/groups")
async def admin_list_groups(request: Request):
    p = await _verify(request)
    _require_owner(p)
    rows = await _db.list_approved_groups()
    return {"items": rows, "count": len(rows)}


@router.post("/api/miniapp/admin/groups/approve")
async def admin_approve_group(request: Request, body: GroupApproveBody):
    p = await _verify(request)
    uid = _require_owner(p)
    if body.chat_id >= 0:
        return JSONResponse({"ok": False,
                             "error": "Group chat_ids are negative. Did you mean to approve a user?"},
                            status_code=400)
    ok = await _db.approve_group(body.chat_id, body.label, uid)
    if not ok:
        return JSONResponse({"ok": False, "error": "Invalid chat_id."},
                            status_code=400)
    return {"ok": True}


@router.post("/api/miniapp/admin/groups/unapprove")
async def admin_unapprove_group(request: Request, body: GroupUnapproveBody):
    p = await _verify(request)
    _require_owner(p)
    ok = await _db.unapprove_group(body.chat_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Group not found."},
                            status_code=404)
    return {"ok": True}


# ── Admin: bot-token rotation drill ──────────────────────────────────────────


@router.get("/api/miniapp/admin/security")
async def admin_security(request: Request):
    p = await _verify(request)
    _require_owner(p)
    return await _auth.get_token_health()


@router.post("/api/miniapp/admin/security/pin")
async def admin_pin_token(request: Request):
    p = await _verify(request)
    _require_owner(p)
    return {"ok": True, **(await _auth.pin_current_token())}


@router.get("/api/miniapp/admin/mode")
async def admin_get_mode(request: Request):
    p = await _verify(request)
    _require_owner(p)
    return await _auth.get_admin_only_mode()


@router.post("/api/miniapp/admin/mode")
async def admin_set_mode(request: Request, body: AdminModeBody):
    p = await _verify(request)
    _require_owner(p)
    await _auth.set_admin_only_mode(body.enabled, body.reason)
    return {"ok": True, **(await _auth.get_admin_only_mode())}


@router.get("/api/miniapp/admin/sites")
async def admin_get_sites(request: Request):
    """Return ALL known platforms + which ones are currently blocked, with
    a `category` tag per platform so the UI can group them (Adult cam vs
    Live streaming vs Social vs Regional (CN), etc.).
    Source of truth for "all platforms" is stream_monitor's hostname map
    (the same lookup used for grouping the watchlist UI)."""
    p = await _verify(request)
    _require_owner(p)
    known = sorted({label for _, label in stream_monitor._PLATFORM_MAP})
    blocked = set(await _auth.get_site_blocklist())
    return {
        "platforms": [
            {
                "name":     k,
                "blocked":  (k in blocked),
                "category": _auth.PLATFORM_CATEGORY.get(k, "Other"),
            }
            for k in known
        ],
        "blocked_count": len(blocked),
        "defaults_seeded": (await _db.get_setting("site_blocklist_seeded", "false")).lower() == "true",
    }


@router.post("/api/miniapp/admin/sites")
async def admin_set_sites(request: Request, body: SiteBlocklistBody):
    p = await _verify(request)
    _require_owner(p)
    persisted = await _auth.set_site_blocklist(body.blocked or [])
    return {"ok": True, "blocked": persisted}


@router.post("/api/miniapp/restart")
async def restart_service(request: Request):
    """Graceful container restart (owner-only). The container's restart_policy
    in docker-compose (unless-stopped) brings it back up automatically. This
    is required for settings whose Python module reads them at import time
    (anything with needs_restart=True)."""
    p = await _verify(request)
    _require_owner(p)
    logger.info("restart_service: SIGTERM scheduled by owner")

    # Defer the SIGTERM by a moment so the HTTP response can flush.
    async def _terminate_later():
        await asyncio.sleep(0.5)
        import signal
        os.kill(1, signal.SIGTERM)
    asyncio.create_task(_terminate_later())
    return {"ok": True, "msg": "Restart scheduled — container will come back up automatically."}


# ── HTML (inline single-page app) ────────────────────────────────────────────


HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>SM-DL</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root {
  --bg: var(--tg-theme-bg-color, #1c1c1e);
  --fg: var(--tg-theme-text-color, #e8e8ea);
  --muted: var(--tg-theme-hint-color, #8e8e93);
  --link: var(--tg-theme-link-color, #2997ff);
  --button: var(--tg-theme-button-color, #2997ff);
  --button-text: var(--tg-theme-button-text-color, #fff);
  --section: var(--tg-theme-section-bg-color, #2c2c2e);
  --separator: var(--tg-theme-section-separator-color, #38383a);
  --destructive: #ff453a;
  --success: #34c759;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body { margin: 0; padding: 0; font: 15px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       background: var(--bg); color: var(--fg); padding-bottom: 70px; min-height: 100vh; }
.tabbar { position: fixed; left: 0; right: 0; bottom: 0; background: var(--section);
          border-top: 1px solid var(--separator); display: flex; height: 58px; z-index: 10; }
.tab { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
       color: var(--muted); cursor: pointer; font-size: 11px; gap: 2px; user-select: none; }
.tab.active { color: var(--button); }
.tab .icon { font-size: 20px; line-height: 1; }
.page { display: none; padding: 12px; }
.page.active { display: block; }
h1 { font-size: 1.3em; margin: 6px 0 14px; }
.card { background: var(--section); border-radius: 10px; padding: 12px; margin-bottom: 10px; }
.row { display: flex; align-items: center; gap: 10px; }
.row .grow { flex: 1; min-width: 0; }
.row .name { font-weight: 600; word-break: break-word; }
.row .meta { font-size: 12px; color: var(--muted); margin-top: 2px; word-break: break-all; }
button { background: var(--button); color: var(--button-text); border: 0; padding: 9px 14px;
         border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; touch-action: manipulation; }
button:active { transform: scale(0.97); }
button.sec { background: transparent; color: var(--button); border: 1px solid var(--button); }
button.danger { background: var(--destructive); }
button.small { padding: 6px 10px; font-size: 12px; }
input { width: 100%; padding: 10px 12px; border: 1px solid var(--separator); border-radius: 8px;
        background: var(--bg); color: var(--fg); font-size: 14px; }
input:focus { outline: none; border-color: var(--button); }
.field { font-size: 12px; color: var(--muted); margin: 4px 4px 4px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; vertical-align: middle; }
.dot.live    { background: var(--success); box-shadow: 0 0 6px var(--success); animation: pulse 1.4s infinite; }
.dot.offline { background: var(--destructive); }
.dot.unknown { background: var(--muted); }
.dot.idle    { background: var(--muted); }
.wl-group-head { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px;
    color: var(--muted); margin: 14px 4px 6px; display: flex; align-items: center; gap: 6px; }
.wl-group-head:first-child { margin-top: 4px; }
.wl-group-count { background: var(--separator); color: var(--muted); border-radius: 10px;
    padding: 1px 7px; font-size: 10px; font-weight: 600; letter-spacing: 0; }
.card.recording { box-shadow: inset 3px 0 0 0 var(--success); }
.rec-tag { color: var(--success); font-weight: 700; letter-spacing: 0.4px; }
.icon-btn.rec-on { color: var(--destructive); border-color: var(--destructive); }
.wl-row { display: flex; align-items: center; gap: 8px; }
.wl-row .grow { flex: 1; min-width: 0; }
.wl-row .username { font-weight: 600; font-size: 15px; }
.wl-row .u-link { color: var(--fg); text-decoration: none; cursor: pointer; -webkit-tap-highlight-color: rgba(41,151,255,0.2); }
.wl-row .u-link:active { color: var(--button); }
.wl-row .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
.wl-row button.icon-btn { background: transparent; color: var(--muted); border: 1px solid var(--separator);
    padding: 5px 9px; font-size: 14px; line-height: 1; border-radius: 6px; min-width: 36px; }
.wl-row button.icon-btn.on { color: #ff9500; border-color: #ff9500; }
.wl-row button.icon-btn:hover { color: var(--button); border-color: var(--button); }
.wl-edit { margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--separator); display: none; }
.wl-edit.open { display: block; }
.wl-edit input { font-size: 12px; padding: 7px 10px; margin-bottom: 6px; }
.wl-edit .row { gap: 6px; }
.wl-edit button { font-size: 12px; padding: 6px 10px; }
.restart-banner { background: rgba(255,149,0,0.15); color: #ff9500; padding: 8px 12px; border-radius: 8px;
    margin: 10px 0; font-size: 12px; display: none; }
.restart-banner.show { display: block; }
.btn-row { display: flex; gap: 8px; margin: 14px 0; }
.btn-row button { flex: 1; }
button.warn { background: #ff9500; color: #fff; }
.tab.admin-only { display: none; }
.tab.admin-only.show { display: flex; }
.lockdown-banner { background: rgba(255,69,58,0.18); color: var(--destructive); padding: 10px 12px;
    border-radius: 8px; margin: 10px 0; font-weight: 600; font-size: 13px; }
.lockdown-banner .reason { font-weight: 400; font-size: 12px; margin-top: 4px; opacity: 0.85; }
.user-row { display: flex; align-items: center; gap: 10px; }
.user-row .meta { font-size: 11px; color: var(--muted); }
.user-row .ban-badge { background: rgba(255,69,58,0.15); color: var(--destructive);
    padding: 2px 7px; border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; }
.user-row .owner-badge { background: rgba(52,199,89,0.15); color: var(--success);
    padding: 2px 7px; border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; }
.site-toggle { display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 8px 0; border-bottom: 1px solid var(--separator); }
.site-toggle:last-child { border-bottom: 0; }
.switch { position: relative; width: 44px; height: 24px; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch .slider { position: absolute; cursor: pointer; inset: 0; background: var(--separator);
    border-radius: 24px; transition: 0.2s; }
.switch .slider::before { content: ''; position: absolute; left: 3px; top: 3px;
    width: 18px; height: 18px; background: #fff; border-radius: 50%; transition: 0.2s; }
.switch input:checked + .slider { background: var(--success); }
.switch input:checked + .slider::before { transform: translateX(20px); }
.switch.danger input:checked + .slider { background: var(--destructive); }
@keyframes pulse { 50% { opacity: 0.5; } }
.empty { text-align: center; color: var(--muted); padding: 40px 20px; font-size: 14px; }
.msg { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.msg.ok { background: rgba(52,199,89,0.15); color: var(--success); }
.msg.err { background: rgba(255,69,58,0.15); color: var(--destructive); }
.platform-pill { display: inline-block; background: var(--bg); border: 1px solid var(--separator);
                 padding: 4px 8px; border-radius: 6px; margin: 2px; font-size: 12px; }
.platform-pill.live { border-color: var(--success); color: var(--success); }
.file-list { font-size: 12px; color: var(--muted); margin-top: 4px; }
.file-list a { color: var(--link); display: block; word-break: break-all; }
.timeago { font-size: 11px; color: var(--muted); }
.spin { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--separator);
        border-top-color: var(--button); border-radius: 50%; animation: sp 0.8s linear infinite;
        vertical-align: middle; }
@keyframes sp { to { transform: rotate(360deg); } }
.url { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; }
</style>
</head><body>

<div id=app>
  <div id=msg></div>

  <div class=page id=page-downloads>
    <h1>Recent Downloads</h1>
    <div id=downloads-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-watchlist>
    <h1>Stream Watchlist</h1>
    <div class=card>
      <div class=field>Streamer / channel URL</div>
      <input id=watch-url placeholder="https://twitch.tv/...">
      <div class=btn-row style="margin-top:8px;gap:6px">
        <button class=sec onclick=testWatchUrl()>🔗 Test</button>
        <button onclick=addWatch()>+ Add to watchlist</button>
      </div>
      <div id=watch-test-result style="margin-top:10px"></div>
      <div id=watch-info-footer class=meta style="margin-top:10px;border-top:1px dashed var(--separator);padding-top:8px">
        📡 Live recording: <b>youtube · twitch · kick</b><br>
        🎥 Anything else: 1700+ sites via yt-dlp
      </div>
    </div>
    <div id=watchlist-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-live>
    <h1>Live Streams</h1>
    <div class=card>
      <div class=field>Start a new recording</div>
      <input id=stream-url placeholder="https://twitch.tv/... (live URL)">
      <div style="margin-top:8px"><button onclick=startStream()>▶ Start recording</button></div>
    </div>
    <div id=live-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-settings>
    <h1>Settings</h1>
    <div id=settings-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-admin>
    <h1>Admin</h1>
    <div id=admin-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>
</div>

<div class=tabbar>
  <div class="tab active" onclick="goto('downloads')"><div class=icon>📥</div><div>Downloads</div></div>
  <div class=tab onclick="goto('watchlist')"><div class=icon>👁</div><div>Watchlist</div></div>
  <div class=tab onclick="goto('live')"><div class=icon>🔴</div><div>Live</div></div>
  <div class=tab onclick="goto('settings')"><div class=icon>⚙️</div><div>Settings</div></div>
  <div class="tab admin-only" id=tab-admin onclick="goto('admin')"><div class=icon>🛡</div><div>Admin</div></div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';
let current = 'downloads';
let liveTimer = null;
let watchlistTimer = null;

function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: {
      'X-Init-Data': initData,
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
  }).then(r => r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || j.error || ('HTTP '+r.status))));
}

function showOk(t) { const m = document.getElementById('msg'); m.className = 'msg ok'; m.textContent = t; setTimeout(()=>m.className='', 3500); }
function showErr(t) { const m = document.getElementById('msg'); m.className = 'msg err'; m.textContent = String(t); setTimeout(()=>m.className='', 5500); }

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function timeago(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  const s = Math.floor((Date.now()-t)/1000);
  if (s<60) return s+'s ago';
  if (s<3600) return Math.floor(s/60)+'m ago';
  if (s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function bytes(n) { if (!n) return '0 B'; const u = ['B','KB','MB','GB']; let i = 0; while (n>=1024 && i<u.length-1) { n/=1024; i++; } return n.toFixed(1)+' '+u[i]; }
function duration(s) { if (s<60) return s+'s'; const m = Math.floor(s/60); const sec = s%60; if (m<60) return m+'m '+sec+'s'; return Math.floor(m/60)+'h '+(m%60)+'m'; }

function goto(page) {
  current = page;
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-'+page));
  const order = ['downloads','watchlist','live','settings','admin'];
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', order[i] === page));
  if (page === 'downloads') loadDownloads();
  else if (page === 'watchlist') loadWatchlist();
  else if (page === 'live') loadLive();
  else if (page === 'settings') loadSettings();
  else if (page === 'admin') loadAdmin();

  // start/stop the live refresh timer
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (page === 'live') liveTimer = setInterval(loadLive, 5000);

  // Watchlist auto-refresh so an in-progress recording's size + duration
  // tick up and a streamer going LIVE flips colour without manual reload.
  if (watchlistTimer) { clearInterval(watchlistTimer); watchlistTimer = null; }
  if (page === 'watchlist') watchlistTimer = setInterval(loadWatchlist, 5000);
}

async function loadDownloads() {
  try {
    const j = await api('/api/miniapp/downloads?limit=50');
    const root = document.getElementById('downloads-list');
    if (!j.items.length) { root.innerHTML = '<div class=empty>No downloads yet.</div>'; return; }
    // Probe OneDrive mode once; the per-row button only renders when not disabled.
    let odMode = 'disabled';
    try {
      const cfg = await api('/api/miniapp/config');
      odMode = (cfg.values && cfg.values.onedrive_mode) || 'disabled';
    } catch(_e) {}
    const showCloud = odMode !== 'disabled';
    root.innerHTML = j.items.map(d => {
      const u = encodeURIComponent(d.url);
      // File link: only show for downloads that have a signed share_url
      // (live recordings + files ≥50 MB). Reels/photos stay compact.
      let fileLine = '';
      if (d.share_url) {
        const filenames = (d.files || []).map(f => f.split('/').pop()).filter(Boolean);
        const fname = filenames[0] || 'file';
        const share = encodeURIComponent(d.share_url);
        const tag = d.is_live_recording ? '🔴' : '🎥';
        const sizeStr = d.size_mb ? ` · ${d.size_mb} MB` : '';
        fileLine = `<div class=meta style="margin-top:4px">
          <a class=u-link onclick="openExternal('${share}')">${tag} ${esc(fname)}${sizeStr}</a>
        </div>`;
      }
      return `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name>
              <a class=u-link onclick="openExternal('${u}')">${esc(d.platform || 'other')} · @${esc(d.uploader || '?')}</a>
            </div>
            ${fileLine}
            <div class=timeago>${timeago(d.downloaded_at || d.created_at)}</div>
          </div>
          ${showCloud ? `<button class="icon-btn" title="Upload to OneDrive"
              onclick="uploadToOneDrive('${u}', this)">☁</button>` : ''}
        </div>
      </div>
    `;}).join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function uploadToOneDrive(encodedUrl, btn) {
  const url = decodeURIComponent(encodedUrl);
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await api('/api/miniapp/onedrive/upload', {
      method: 'POST', body: JSON.stringify({url}),
    });
    if (r.failed_count) {
      showErr(`Uploaded ${r.sent_count}, ${r.failed_count} failed`);
    } else {
      showOk(`Uploaded ${r.sent_count} file${r.sent_count===1?'':'s'} · ${bytes(r.total_bytes)}`);
    }
    btn.textContent = '✓';
  } catch(e) {
    showErr(e); btn.textContent = original; btn.disabled = false;
  }
}

function statusLabel(s) {
  if (s === 'live') return 'LIVE';
  if (s === 'offline') return 'offline';
  return 'unknown';
}

// Platform → emoji prefix for group headers. Falls through to a generic icon.
const PLATFORM_ICON = {
  'Chaturbate': '🎥', 'Stripchat': '🎥', 'BongaCams': '🎥', 'Cam4': '🎥',
  'Twitch': '🎮', 'Kick': '🥊', 'YouTube': '▶', 'Instagram': '📷',
  'TikTok': '🎵', 'Twitter/X': '𝕏', 'Facebook': '👤', 'Reddit': '🤖',
  'Vimeo': '🎞', 'Rumble': '🎬', 'DLive': '📡', 'Trovo': '📡',
  'Bilibili': '📺', 'Douyu': '📺',
};

async function loadWatchlist() {
  try {
    const j = await api('/api/miniapp/watchlist');
    const root = document.getElementById('watchlist-list');
    if (!j.items.length) { root.innerHTML = '<div class=empty>Watchlist is empty.</div>'; return; }
    const active = j.active || {};

    // Group items by platform (already sorted alphabetically server-side
    // by platform then username, so preserve insertion order here).
    const groups = new Map();
    for (const w of j.items) {
      const k = w.platform || 'Other';
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(w);
    }

    let idx = 0;
    const sections = [];
    for (const [platform, rows] of groups) {
      const icon = PLATFORM_ICON[platform] || '🌐';
      const head = `<div class=wl-group-head>${icon} ${esc(platform)}
                      <span class=wl-group-count>${rows.length}</span></div>`;
      const body = rows.map(w => {
        const i = idx++;
        const status = w.status || 'unknown';
        const muted  = !!w.muted;
        const u = encodeURIComponent(w.url);
        const muteTitle = muted ? 'Muted — tap to unmute' : 'Mute notifications';
        const muteIcon  = muted ? '🔕' : '🔔';
        const job = active[w.url];
        const recording = !!job;
        // Status sub-line: "LIVE · 12m 34s · 145.3 MB" when recording, plain
        // "LIVE"/"offline"/"unknown" otherwise.
        let sub;
        if (recording) {
          sub = `<span class=rec-tag>● REC</span> · LIVE · ${duration(job.elapsed_sec)} · ${bytes(job.bytes)}`;
        } else {
          sub = statusLabel(status)
              + (w.label && w.label !== w.url ? ' · ' + esc(w.label) : '')
              + (muted ? ' · 🔕 muted' : '');
        }
        // Action button: ⏹ Stop while recording, ▶ Rec otherwise.
        const actionBtn = recording
          ? `<button class="icon-btn rec-on" title="Stop recording"
                     onclick="stopFromWatchlist(${job.chat_id})">⏹</button>`
          : `<button class="icon-btn" title="Start recording"
                     onclick="recFromWatchlist('${u}')">▶</button>`;
        return `
        <div class="card ${recording?'recording':''}">
          <div class=wl-row>
            <span class="dot ${esc(status)}" title="${esc(statusLabel(status))}"></span>
            <div class=grow>
              <div class=username><a class=u-link onclick="openExternal('${u}')">${esc(w.username || w.url)}</a></div>
              <div class=sub>${sub}</div>
            </div>
            ${actionBtn}
            <button class="icon-btn ${muted?'on':''}" title="${esc(muteTitle)}"
                    onclick="toggleMute('${u}', ${muted?'false':'true'})">${muteIcon}</button>
            <button class="icon-btn" title="Edit URL" onclick="toggleEdit(${i})">✎</button>
            <button class="icon-btn" title="Remove" onclick="removeWatch('${u}')">🗑</button>
          </div>
          <div class="wl-edit" id="wl-edit-${i}">
            <div class=field>URL</div>
            <input id="wl-url-${i}" value="${esc(w.url)}">
            <div class=field>Label (optional)</div>
            <input id="wl-label-${i}" value="${esc(w.label || '')}">
            <div class=row>
              <button class=sec onclick="toggleEdit(${i})">Cancel</button>
              <button onclick="saveEdit(${i}, '${u}')">Save</button>
            </div>
          </div>
        </div>`;
      }).join('');
      sections.push(head + body);
    }
    root.innerHTML = sections.join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function recFromWatchlist(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  try {
    await api('/api/miniapp/stream/start', {
      method: 'POST',
      body: JSON.stringify({url}),
    });
    showOk('Recording queued · ' + url);
    setTimeout(loadWatchlist, 1200);
  } catch(e) { showErr(e); }
}

async function stopFromWatchlist(chat_id) {
  try {
    const j = await api('/api/miniapp/stream/stop', {
      method: 'POST',
      body: JSON.stringify({chat_id}),
    });
    showOk('Stop sent · ' + duration(j.status.elapsed_seconds));
    setTimeout(loadWatchlist, 1000);
  } catch(e) { showErr(e); }
}

function toggleEdit(i) {
  const el = document.getElementById('wl-edit-' + i);
  if (el) el.classList.toggle('open');
}

async function saveEdit(i, encodedOldUrl) {
  const oldUrl = decodeURIComponent(encodedOldUrl);
  const newUrl = document.getElementById('wl-url-'   + i).value.trim();
  const label  = document.getElementById('wl-label-' + i).value.trim();
  if (!newUrl) { showErr('URL cannot be empty'); return; }
  try {
    await api('/api/miniapp/watchlist/edit', {
      method: 'POST',
      body: JSON.stringify({url: oldUrl, new_url: newUrl, label: label}),
    });
    showOk('Updated');
    loadWatchlist();
  } catch(e) { showErr(e); }
}

function openExternal(encodedUrl) {
  // Open the URL in the user's external browser. Inside Telegram, prefer
  // tg.openLink (gives the user the "Open in Chrome / Safari" prompt with
  // their default browser); fall back to window.open elsewhere.
  let url = decodeURIComponent(encodedUrl);
  // Defensive: if the stored URL has no scheme (e.g. "www.twitch.tv/foo"),
  // tg.openLink treats it as a relative path → resolves against the Mini
  // App's own origin → 404. Add https:// when scheme is missing.
  const lc = url.toLowerCase();
  if (!(lc.startsWith('http://') || lc.startsWith('https://'))) {
    url = 'https://' + url.replace(/^\/+/, '');
  }
  try {
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, '_blank', 'noopener,noreferrer');
  } catch(e) {
    window.open(url, '_blank', 'noopener,noreferrer');
  }
}

async function toggleMute(encodedUrl, muted) {
  const url = decodeURIComponent(encodedUrl);
  try {
    await api('/api/miniapp/watchlist/mute', {
      method: 'POST',
      body: JSON.stringify({url, muted: (muted === 'true' || muted === true)}),
    });
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function addWatch() {
  const url = document.getElementById('watch-url').value.trim();
  if (!url) { showErr('URL required'); return; }
  try {
    // Label is now auto-extracted server-side from the URL (e.g.
    // chaturbate.com/dewdropdoll → dewdropdoll). Rename later via the
    // ✎ button on the row if you want something custom.
    await api('/api/miniapp/watchlist/add', { method: 'POST', body: JSON.stringify({url, label: null}) });
    showOk('Added');
    document.getElementById('watch-url').value = '';
    const r = document.getElementById('watch-test-result');
    if (r) r.innerHTML = '';
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function testWatchUrl() {
  const inputEl = document.getElementById('watch-url');
  const resultEl = document.getElementById('watch-test-result');
  const url = (inputEl?.value || '').trim();
  if (!url) { showErr('Paste a URL first.'); return; }
  resultEl.innerHTML = '<div class=meta><span class=spin></span> Probing…</div>';
  try {
    const r = await api('/api/miniapp/test_url', {
      method: 'POST', body: JSON.stringify({url}),
    });
    if (!r.ok) {
      resultEl.innerHTML = `<div class=meta style="color:var(--destructive)">${esc(r.error || 'Failed')}</div>`;
      return;
    }
    const ok = '<span style="color:var(--success)">✓</span>';
    const no = '<span style="color:var(--destructive)">✗</span>';
    const q = '<span style="color:#ff9500">?</span>';
    const lines = [];
    lines.push(`${r.recognised ? ok : q} <b>${esc(r.platform || 'unknown')}</b>${r.recognised ? '' : ' <span class=meta>(unknown site)</span>'}`);
    if (r.recognised && r.platform !== 'private category') {
      lines.push(`${r.live_supported ? ok : no} Live recording ${r.live_supported ? 'supported' : 'not supported'}`);
    }
    lines.push(`${r.available ? ok : no} ${r.available ? 'Available to you' : 'Not available'}`);
    if (r.reason) lines.push(`<div class=meta style="margin-top:6px">${esc(r.reason)}</div>`);
    resultEl.innerHTML = lines.map(l => `<div style="margin:4px 0">${l}</div>`).join('');
  } catch(e) {
    resultEl.innerHTML = `<div class=meta style="color:var(--destructive)">${esc(String(e))}</div>`;
  }
}

async function removeWatch(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  if (tg?.showConfirm) {
    tg.showConfirm('Remove this from watchlist?', async (ok) => { if (ok) await _doRemoveWatch(url); });
  } else if (confirm('Remove ' + url + '?')) {
    await _doRemoveWatch(url);
  }
}

async function _doRemoveWatch(url) {
  try {
    await api('/api/miniapp/watchlist/remove', { method: 'POST', body: JSON.stringify({url}) });
    showOk('Removed');
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function loadLive() {
  try {
    const j = await api('/api/miniapp/active');
    const root = document.getElementById('live-list');
    if (!j.items.length) { root.innerHTML = '<div class=empty>No active recordings.</div>'; return; }
    root.innerHTML = j.items.map(s => `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name><span class="dot live"></span>${esc(s.platform || 'recording')} · @${esc(s.uploader || '?')}</div>
            <div class=meta>${duration(s.elapsed_sec)} · ${bytes(s.bytes)} ${s.stop_requested_at ? '· stopping…' : ''}</div>
            <div class="meta url">${esc(s.url)}</div>
          </div>
          <button class="small danger" onclick="stopStream(${s.chat_id})">⏹ Stop</button>
        </div>
      </div>
    `).join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function startStream() {
  const url = document.getElementById('stream-url').value.trim();
  if (!url) { showErr('URL required'); return; }
  try {
    const j = await api('/api/miniapp/stream/start', { method: 'POST', body: JSON.stringify({url}) });
    showOk('Recording queued · @' + (j.url||''));
    document.getElementById('stream-url').value = '';
    setTimeout(loadLive, 1500);
  } catch(e) { showErr(e); }
}

async function stopStream(chat_id) {
  try {
    const j = await api('/api/miniapp/stream/stop', { method: 'POST', body: JSON.stringify({chat_id}) });
    showOk('Stop sent · ' + duration(j.status.elapsed_seconds));
    setTimeout(loadLive, 1000);
  } catch(e) { showErr(e); }
}

// The old loadSites/testSiteUrl functions were removed when the Sites tab
// was consolidated into the Watchlist add card. testWatchUrl() replaces
// testSiteUrl(); the Sites tab no longer exists.

function _renderSettingField(s, v, idPrefix) {
  const id = idPrefix + s.key;
  let input;
  if (s.type === 'choice') {
    input = `<select id="${id}">${s.choices.map(c => `<option ${c===v?'selected':''} value="${esc(c)}">${esc(c)}</option>`).join('')}</select>`;
  } else if (s.type === 'bool') {
    input = `<select id="${id}"><option value=true ${v?'selected':''}>Yes</option><option value=false ${!v?'selected':''}>No</option></select>`;
  } else if (s.type === 'string') {
    input = `<input id="${id}" type=text value="${esc(v ?? '')}">`;
  } else {
    input = `<input id="${id}" type=number ${s.min!=null?'min='+s.min:''} ${s.max!=null?'max='+s.max:''} value="${v ?? ''}">`;
  }
  const restart = s.needs_restart ? ' <span style="color:#ff9500;font-size:11px">· restart required</span>' : '';
  return `<div class=card>
    <div class=field>${esc(s.label)}${restart}</div>
    ${input}
  </div>`;
}

async function loadSettings() {
  const root = document.getElementById('settings-content');
  root.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  try {
    const cfg = await api('/api/miniapp/config');
    // Settings tab = only NON-admin keys. Admin keys live on the Admin tab.
    const visible = cfg.settings.filter(s => !s.admin);
    const fields = visible.map(s => _renderSettingField(s, cfg.values[s.key], 'set-')).join('');
    const disk = cfg.disk;
    const diskHtml = disk.free_gb != null
      ? `<div class=meta>${disk.free_gb} GB free of ${disk.total_gb} GB · ${disk.used_gb} GB used</div>`
      : `<div class=meta>(disk usage unavailable)</div>`;
    root.innerHTML = `
      ${fields}
      <div class=restart-banner id=restart-banner>
        ⚠ Some settings require a service restart to take effect.
      </div>
      <div class=btn-row>
        <button onclick="saveSettings('set-')">💾 Save changes</button>
      </div>

      <div class=card>
        <div class=field>Downloads folder (env var, container)</div>
        <div class=meta><span class=url>${esc(cfg.paths.downloads_dir)}</span>
          ${cfg.paths.downloads_dir_writable ? '<span style="color:var(--success)">· writable</span>' : '<span style="color:var(--destructive)">· not writable</span>'}</div>
        ${diskHtml}
        <div class=meta style="margin-top:6px">To change: edit <code>DOWNLOADS_DIR</code> in docker-compose and restart the container.</div>
      </div>
    `;
  } catch(e) { showErr('Load failed: '+e); }
}

async function saveSettings(prefix) {
  prefix = prefix || 'set-';
  const cfg = await api('/api/miniapp/config');
  const updates = {};
  for (const s of cfg.settings) {
    const el = document.getElementById(prefix + s.key);
    if (!el) continue;
    let v = el.value;
    if (s.type === 'int') v = parseInt(v, 10);
    else if (s.type === 'bool') v = (v === 'true' || v === true);
    updates[s.key] = v;
  }
  try {
    const j = await api('/api/miniapp/config', { method: 'POST', body: JSON.stringify({updates}) });
    const inAdmin = (prefix === 'adm-');
    if (j.needs_restart && j.needs_restart.length) {
      showOk('Saved · restart required for: ' + j.needs_restart.join(', '));
      if (inAdmin) {
        await loadAdmin();
        const banner = document.getElementById('admin-restart-banner');
        if (banner) {
          banner.classList.add('show');
          banner.textContent = '⚠ Restart required for: ' + j.needs_restart.join(', ');
        }
      } else {
        await loadSettings();
        const banner = document.getElementById('restart-banner');
        if (banner) {
          banner.classList.add('show');
          banner.textContent = '⚠ Restart required for: ' + j.needs_restart.join(', ');
        }
      }
    } else {
      showOk('Saved');
      inAdmin ? loadAdmin() : loadSettings();
    }
  } catch(e) {
    if (Array.isArray(e)) showErr(e.join(' · '));
    else showErr(e.errors ? e.errors.join(' · ') : e);
  }
}

// ── Admin tab ─────────────────────────────────────────────────────────────

let isOwner = false;
let adminBootstrapped = false;

async function bootstrapWhoami() {
  try {
    const j = await api('/api/miniapp/whoami');
    isOwner = !!j.is_owner;
    const tabA = document.getElementById('tab-admin');
    if (tabA) tabA.classList.toggle('show', isOwner);
  } catch(e) { /* owner-flag is best-effort; tab stays hidden on failure */ }
}

async function loadAdmin() {
  if (!isOwner) {
    document.getElementById('admin-content').innerHTML =
      '<div class=empty>Admin tab is owner-only.</div>';
    return;
  }
  const root = document.getElementById('admin-content');
  root.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  try {
    const [mode, users, groups, sites, od, cfg] = await Promise.all([
      api('/api/miniapp/admin/mode'),
      api('/api/miniapp/admin/users'),
      api('/api/miniapp/admin/groups'),
      api('/api/miniapp/admin/sites'),
      api('/api/miniapp/onedrive/status'),
      api('/api/miniapp/config'),
    ]);

    // 1. Admin-only-mode (kill switch)
    const modeHtml = `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name>🔒 Admin-only session</div>
            <div class=meta>When enabled, only you can use the bot/Mini App. Anyone else gets a "service in admin-only mode" notice.</div>
          </div>
          <label class="switch danger">
            <input type=checkbox id=admin-mode-toggle ${mode.enabled?'checked':''} onchange="setAdminMode(this.checked)">
            <span class=slider></span>
          </label>
        </div>
        <div class=field style="margin-top:10px">Reason (optional, shown to no one — for your records)</div>
        <input id=admin-mode-reason placeholder="e.g. investigating activity 2026-05-16" value="${esc(mode.reason || '')}">
        <div style="margin-top:8px"><button class=sec onclick=saveAdminModeReason()>Save reason</button></div>
      </div>`;

    // 2a. Pending approvals — show the access codes; legitimate path is
    //     "user DM's me the code, I paste it into the input below". The
    //     per-row Approve button is for cases where I already know who's
    //     who and just want to promote them by chat_id.
    const pending = users.items.filter(u => u.status === 'pending');
    const pendingHtml = `
      <div class=card>
        <div class=field>🔓 Pending approvals (${pending.length})</div>
        <div class=meta style="margin-bottom:8px">Paste the 9-digit code a user sent you out-of-band:</div>
        <input id=approve-code-input placeholder="123-456-789" style="font-family:ui-monospace;font-size:16px;letter-spacing:1px">
        <div style="margin-top:8px"><button onclick=approveByCode()>✅ Approve by code</button></div>
        ${pending.length === 0
          ? '<div class=meta style="margin-top:10px">No pending users.</div>'
          : pending.map(u => {
              const handle = u.username ? '@' + u.username : (u.first_name || ('chat ' + u.chat_id));
              const codeStr = u.pending_code || '(no code)';
              const expired = u.pending_expires_at && (new Date(u.pending_expires_at) < new Date());
              return `
              <div class="user-row" style="padding:10px 0;border-top:1px solid var(--separator)">
                <div class=grow>
                  <div class=name>${esc(handle)} <span class=ban-badge style="margin-left:6px;background:rgba(255,149,0,0.18);color:#ff9500">PENDING</span></div>
                  <div class=meta>chat_id ${u.chat_id} · ${u.interaction_count}× · last /start ${timeago(u.last_seen)}</div>
                  <div class=meta style="font-family:ui-monospace;color:${expired?'var(--destructive)':'var(--fg)'}">
                    code ${esc(codeStr)} ${expired ? '· EXPIRED' : ''}
                  </div>
                </div>
                <button onclick="approveUser(${u.chat_id})">Approve</button>
              </div>`;
            }).join('')}
      </div>`;

    // 2b. Existing users (active + banned)
    const others = users.items.filter(u => u.status !== 'pending');
    const usersHtml = `
      <div class=card>
        <div class=field>👥 Users (${others.length})</div>
        ${others.length === 0
          ? '<div class=meta>No approved users yet.</div>'
          : others.map(u => {
              const banned = (u.status === 'banned');
              const owner = !!u.is_owner;
              const handle = u.username ? '@' + u.username : (u.first_name || ('chat ' + u.chat_id));
              return `
              <div class="user-row" style="padding:10px 0;border-top:1px solid var(--separator)">
                <div class=grow>
                  <div class=name>${esc(handle)}
                    ${owner ? '<span class=owner-badge style="margin-left:6px">OWNER</span>' : ''}
                    ${banned ? '<span class=ban-badge style="margin-left:6px">BANNED</span>' : ''}
                  </div>
                  <div class=meta>chat_id ${u.chat_id} · ${u.interaction_count}× · last seen ${timeago(u.last_seen)}</div>
                  ${u.banned_reason ? `<div class=meta>Reason: ${esc(u.banned_reason)}</div>` : ''}
                </div>
                ${owner ? '' : (banned
                  ? `<button class=sec onclick="unbanUser(${u.chat_id})">Unban</button>`
                  : `<button class="small danger" onclick="banUser(${u.chat_id})">Ban</button>`)}
              </div>`;
            }).join('')}
      </div>`;

    // 2c. Approved groups — Telegram groups the owner trusts. Members can
    //     use the bot without per-user codes; bot replies are visible to
    //     the whole group.
    const groupsHtml = `
      <div class=card>
        <div class=field>👥 Approved groups (${groups.count})</div>
        <div class=meta style="margin-bottom:8px">Add a group by chat ID (negative number). Send /start in the target group to see its ID.</div>
        <div class=row style="gap:6px">
          <input id=group-chat-id placeholder="-1001234567890" style="flex:1;font-family:ui-monospace">
          <input id=group-label placeholder="Label (e.g. Friends)" style="flex:1">
        </div>
        <div style="margin-top:8px"><button onclick=approveGroup()>+ Approve group</button></div>
        ${groups.items.length === 0
          ? '<div class=meta style="margin-top:10px">No approved groups yet.</div>'
          : groups.items.map(g => `
              <div class="user-row" style="padding:10px 0;border-top:1px solid var(--separator)">
                <div class=grow>
                  <div class=name>${esc(g.label || '(no label)')}</div>
                  <div class=meta>chat_id ${g.chat_id} · added ${timeago(g.approved_at)}</div>
                </div>
                <button class="small danger" onclick="unapproveGroup(${g.chat_id})">Remove</button>
              </div>`).join('')}
      </div>`;

    // 3. Site management — toggles grouped by category for clarity.
    // Order: Adult cam first (so it's at the top with sensitive defaults
    // visible), then Video, Live streaming, Social, Regional, Other.
    const CAT_ORDER = ['Adult cam', 'Live streaming', 'Video', 'Social', 'Regional (CN)', 'Other'];
    const CAT_ICON = {
      'Adult cam':     '🔞',
      'Live streaming':'📡',
      'Video':         '▶',
      'Social':        '📱',
      'Regional (CN)': '🇨🇳',
      'Other':         '🌐',
    };
    const byCat = new Map();
    for (const p of sites.platforms) {
      const c = p.category || 'Other';
      if (!byCat.has(c)) byCat.set(c, []);
      byCat.get(c).push(p);
    }
    const orderedCats = CAT_ORDER.filter(c => byCat.has(c))
      .concat([...byCat.keys()].filter(c => !CAT_ORDER.includes(c)));
    const siteSections = orderedCats.map(cat => {
      const rows = byCat.get(cat).map(p => `
        <div class=site-toggle>
          <div>${esc(p.name)}</div>
          <label class=switch>
            <input type=checkbox ${p.blocked ? '' : 'checked'}
                   onchange="toggleSite('${esc(p.name)}', this.checked)">
            <span class=slider></span>
          </label>
        </div>`).join('');
      return `<div class=wl-group-head>${CAT_ICON[cat] || '🌐'} ${esc(cat)}</div>${rows}`;
    }).join('');
    const sitesHtml = `
      <div class=card>
        <div class=field>🌐 Site allowlist (toggle off to hide from non-owner users)</div>
        ${siteSections || '<div class=meta>No known platforms yet.</div>'}
        <div class=meta style="margin-top:8px">${sites.blocked_count} site${sites.blocked_count===1?'':'s'} currently blocked. Owner is never affected.</div>
      </div>`;

    // 4. Server settings (admin-flagged keys)
    const adminFields = cfg.settings.filter(s => s.admin)
      .map(s => _renderSettingField(s, cfg.values[s.key], 'adm-')).join('');
    const settingsHtml = `
      <div class=card>
        <div class=field>⚙ Server settings (owner-only)</div>
      </div>
      ${adminFields}
      <div class=restart-banner id=admin-restart-banner>
        ⚠ Some settings require a service restart to take effect.
      </div>
      <div class=btn-row>
        <button onclick="saveSettings('adm-')">💾 Save changes</button>
        <button class=warn onclick=restartService()>♻ Restart service</button>
      </div>`;

    // 5. OneDrive (admin-only) — real connect flow.
    let odBody;
    if (od.device_flow) {
      // Mid-authorization: show the code + URL prominently. Background poll
      // hits ONLY /onedrive/status (not the 6-endpoint loadAdmin sweep) and
      // only triggers a full re-render when status actually flips.
      odBody = `
        <div class=name>⏳ Awaiting authorization</div>
        <div style="margin-top:10px;padding:10px;background:var(--bg);border-radius:8px">
          <div class=meta>Open this URL on any device:</div>
          <div style="margin:6px 0;font-size:14px;word-break:break-all">
            <a href="${esc(od.device_flow.verification_uri)}" target=_blank>${esc(od.device_flow.verification_uri)}</a>
          </div>
          <div class=meta>Enter this code:</div>
          <div style="font-family:ui-monospace;font-size:22px;letter-spacing:3px;font-weight:700;margin-top:4px">
            ${esc(od.device_flow.user_code)}
          </div>
          <div class=meta style="margin-top:6px">Expires in <span id=od-expires>${od.device_flow.expires_in}</span>s. Page will update automatically.</div>
        </div>`;
      if (!window._odPoll) {
        window._odPoll = setInterval(_pollOneDriveDuringConnect, 3000);
        setTimeout(() => { if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; } }, 12*60*1000);
      }
    } else if (od.configured) {
      if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
      const q = od.quota;
      odBody = `
        <div class=name>✅ Connected · ${esc(od.account || od.display_name || '?')}</div>
        ${q ? `<div class=meta style="margin-top:4px">${q.free_gb} GB free of ${q.total_gb} GB · ${q.used_gb} GB used ${q.state ? '· ' + esc(q.state) : ''}</div>` : ''}
        <div class=meta>app …${esc(od.client_id_tail)} ${od.token_valid ? '· token healthy' : '· ⚠ refresh failed'}</div>
        <div class=btn-row style="margin-top:8px">
          <button class=sec onclick=testOneDrive()>🧪 Test upload</button>
          <button class="small danger" onclick=disconnectOneDrive()>Disconnect</button>
        </div>`;
    } else {
      if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
      odBody = `
        <div class=name>⚪ Not connected</div>
        <div class=meta style="margin-top:4px">Azure app …${esc(od.client_id_tail)} · Files.ReadWrite scope</div>
        ${od.last_error ? `<div class=meta style="color:var(--destructive);margin-top:4px">Last error: ${esc(od.last_error)}</div>` : ''}
        <div style="margin-top:8px"><button onclick=connectOneDrive()>🔗 Connect OneDrive</button></div>
        <div class=meta style="margin-top:6px">You'll get a 6-character code to type at microsoft.com/devicelogin.</div>`;
    }
    const odHtml = `
      <div class=card>
        <div class=field>📁 OneDrive integration</div>
        ${odBody}
      </div>`;

    // 6. Bot-token security card (rotation drift detector)
    let securityHtml = '';
    try {
      const sec = await api('/api/miniapp/admin/security');
      const badge =
        sec.status === 'in_sync' ? '<span style="color:var(--success)">✓ in sync</span>'
        : sec.status === 'drift' ? '<span style="color:var(--destructive)">⚠ DRIFT — env token does not match pinned hash</span>'
        : '<span style="color:#ff9500">⚪ unpinned — pin the current token to enable drift detection</span>';
      securityHtml = `
      <div class=card>
        <div class=field>🔐 Bot token health</div>
        <div class=name>${badge}</div>
        <div class=meta style="margin-top:4px">
          live token …${esc(sec.live_hash || '(unset)')}
          ${sec.pinned_hash ? '· pinned …' + esc(sec.pinned_hash) : ''}
          ${sec.pinned_at ? '· pinned ' + timeago(sec.pinned_at) : ''}
        </div>
        <div style="margin-top:8px"><button class=sec onclick=pinToken()>📌 Pin current token</button></div>
        <details style="margin-top:10px;font-size:12px;color:var(--muted)">
          <summary style="cursor:pointer">Rotation procedure</summary>
          <ol style="padding-left:20px;margin-top:6px;line-height:1.5">
            <li>BotFather → /mybots → SM-DL → API Token → Revoke current token</li>
            <li>Copy the new token</li>
            <li>Update SMDL_BOT_TOKEN in WCM + .env.local (run sync_env_from_wcm.ps1)</li>
            <li><code>docker compose restart smdl</code></li>
            <li>Return here, hit "Pin current token"</li>
          </ol>
        </details>
      </div>`;
    } catch(_e) { /* security card best-effort */ }

    root.innerHTML =
      (mode.enabled ? `<div class=lockdown-banner>🔒 Admin-only session is ACTIVE${mode.reason ? `<div class=reason>${esc(mode.reason)}</div>` : ''}</div>` : '')
      + modeHtml + pendingHtml + usersHtml + groupsHtml + sitesHtml + securityHtml + settingsHtml + odHtml;
  } catch(e) { showErr('Load failed: ' + e); }
}

async function approveUser(chat_id) {
  try {
    await api('/api/miniapp/admin/users/approve', {
      method: 'POST', body: JSON.stringify({chat_id}),
    });
    showOk('Approved');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function approveByCode() {
  const input = document.getElementById('approve-code-input');
  const code = (input?.value || '').trim();
  if (!code) { showErr('Paste the 9-digit code'); return; }
  try {
    const r = await api('/api/miniapp/admin/users/approve_by_code', {
      method: 'POST', body: JSON.stringify({code}),
    });
    const who = r.username ? '@' + r.username : (r.first_name || ('chat ' + r.chat_id));
    showOk('Approved ' + who);
    if (input) input.value = '';
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function approveGroup() {
  const idEl = document.getElementById('group-chat-id');
  const labelEl = document.getElementById('group-label');
  const chat_id = parseInt((idEl?.value || '').trim(), 10);
  const label = (labelEl?.value || '').trim() || null;
  if (!chat_id || chat_id >= 0) {
    showErr('Group chat IDs are negative numbers (e.g. -1001234567890)');
    return;
  }
  try {
    await api('/api/miniapp/admin/groups/approve', {
      method: 'POST', body: JSON.stringify({chat_id, label}),
    });
    showOk('Group approved');
    if (idEl) idEl.value = '';
    if (labelEl) labelEl.value = '';
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function unapproveGroup(chat_id) {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Revoke this group’s access?', ok => res(!!ok));
    else res(confirm('Revoke this group’s access?'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/admin/groups/unapprove', {
      method: 'POST', body: JSON.stringify({chat_id}),
    });
    showOk('Group revoked');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function connectOneDrive() {
  try {
    const r = await api('/api/miniapp/onedrive/connect', { method: 'POST', body: '{}' });
    showOk('Code issued: ' + r.user_code);
    loadAdmin();  // re-render to surface the code in the OneDrive card
  } catch(e) { showErr(e); }
}

// Lightweight poll during the device-flow window. Hits only /onedrive/status
// (one endpoint vs loadAdmin's six), tweaks the countdown in place, and
// triggers a full loadAdmin() only when the state actually transitions
// (configured / error) — so the page stops feeling like it's reloading.
async function _pollOneDriveDuringConnect() {
  try {
    const s = await api('/api/miniapp/onedrive/status');
    if (s.configured || s.last_error) {
      // Transition — do a full re-render so the success / error UI appears.
      if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
      loadAdmin();
      if (s.configured) showOk('OneDrive connected');
      else if (s.last_error) showErr('OneDrive: ' + s.last_error);
      return;
    }
    // Still pending: just refresh the countdown in place; don't redraw.
    const exp = document.getElementById('od-expires');
    if (exp && s.device_flow && s.device_flow.expires_in != null) {
      exp.textContent = s.device_flow.expires_in;
    }
  } catch(e) {
    // Don't spam the toast on transient polling errors; just log.
    console.warn('OneDrive poll:', e);
  }
}

async function disconnectOneDrive() {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Disconnect OneDrive? The refresh token will be wiped — reconnect later if needed.', ok => res(!!ok));
    else res(confirm('Disconnect OneDrive?'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/onedrive/disconnect', { method: 'POST', body: '{}' });
    showOk('Disconnected');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function testOneDrive() {
  try {
    const r = await api('/api/miniapp/onedrive/test_upload', { method: 'POST', body: '{}' });
    showOk('Uploaded ' + (r.name || 'healthcheck'));
  } catch(e) { showErr(e); }
}

async function pinToken() {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Pin the current SMDL_BOT_TOKEN hash? Do this only after a deliberate rotation.', ok => res(!!ok));
    else res(confirm('Pin the current SMDL_BOT_TOKEN hash?'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/admin/security/pin', { method: 'POST', body: '{}' });
    showOk('Token hash pinned');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function setAdminMode(enabled) {
  const reasonEl = document.getElementById('admin-mode-reason');
  const reason = reasonEl ? reasonEl.value.trim() : '';
  try {
    await api('/api/miniapp/admin/mode', {
      method: 'POST', body: JSON.stringify({enabled, reason}),
    });
    showOk(enabled ? '🔒 Admin-only mode ON' : '🔓 Admin-only mode OFF');
    loadAdmin();
  } catch(e) { showErr(e); loadAdmin(); }
}

async function saveAdminModeReason() {
  const enabledEl = document.getElementById('admin-mode-toggle');
  const enabled = !!(enabledEl && enabledEl.checked);
  const reason = document.getElementById('admin-mode-reason').value.trim();
  try {
    await api('/api/miniapp/admin/mode', {
      method: 'POST', body: JSON.stringify({enabled, reason}),
    });
    showOk('Reason saved');
  } catch(e) { showErr(e); }
}

async function banUser(chat_id) {
  const reason = prompt('Reason for ban (optional, internal):') || '';
  try {
    await api('/api/miniapp/admin/users/ban', {
      method: 'POST', body: JSON.stringify({chat_id, reason}),
    });
    showOk('Banned');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function unbanUser(chat_id) {
  try {
    await api('/api/miniapp/admin/users/unban', {
      method: 'POST', body: JSON.stringify({chat_id}),
    });
    showOk('Unbanned');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function toggleSite(name, enabled) {
  try {
    // Read current state, flip this one, persist.
    const sites = await api('/api/miniapp/admin/sites');
    const next = new Set(sites.platforms.filter(p => p.blocked).map(p => p.name));
    if (enabled) next.delete(name); else next.add(name);
    await api('/api/miniapp/admin/sites', {
      method: 'POST', body: JSON.stringify({blocked: [...next]}),
    });
    showOk(enabled ? name + ' enabled' : name + ' blocked');
  } catch(e) { showErr(e); loadAdmin(); }
}

async function restartService() {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Restart the SM-DL service now? Active recordings will be interrupted.', ok => res(!!ok));
    else res(confirm('Restart the SM-DL service now? Active recordings will be interrupted.'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/restart', { method: 'POST', body: '{}' });
    showOk('Restart scheduled · service will be back in ~5s');
  } catch(e) { showErr(e); }
}

// Surface the Admin tab if we're owner. Best-effort — failures stay silent.
bootstrapWhoami();
goto('downloads');
</script>
</body></html>"""


@router.get("/app", response_class=HTMLResponse)
async def miniapp_index():
    return HTMLResponse(HTML)


@router.get("/app/", response_class=HTMLResponse)
async def miniapp_index_slash():
    return HTMLResponse(HTML)
