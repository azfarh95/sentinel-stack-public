"""Central authorization gate.

One function — `is_authorized(chat_id)` — answers "may this Telegram user
use the bot or the Mini App?" so both interfaces (bot.py handlers and
miniapp.py routes) stay in sync.

Auth model (intentionally permissive by default, owner-controllable):

    1. Owner (OWNER_CHAT_ID) is always allowed, no matter what.
    2. If admin-only mode is ON, only owner is allowed (kill switch).
    3. If the user's status in the `users` table is 'banned' → denied.
    4. Otherwise → allowed.  Implicit allow-on-first-interaction; the bot
       just needs to have seen them once (record_interaction populates the
       row on every message). New users go straight in with status='active'.

Admin-only mode is stored in the existing key-value `settings` table under
key 'admin_only_mode' ('true' / 'false'). The optional reason for the
lockdown is in 'admin_only_reason'.
"""
from __future__ import annotations

import json
from typing import Literal

from .config import OWNER_CHAT_ID
from . import database as _db
from . import stream_monitor as _sm


AuthResult = Literal["allow", "deny_banned", "deny_pending",
                     "deny_admin_only", "deny_owner_required",
                     "deny_unknown"]


async def get_admin_only_mode() -> dict:
    """Returns {'enabled': bool, 'reason': str|None}."""
    enabled = (await _db.get_setting("admin_only_mode", "false")).lower() == "true"
    reason  = (await _db.get_setting("admin_only_reason", "")) or None
    return {"enabled": enabled, "reason": reason}


async def set_admin_only_mode(enabled: bool, reason: str | None = None) -> None:
    await _db.set_setting("admin_only_mode", "true" if enabled else "false")
    if reason is not None:
        await _db.set_setting("admin_only_reason", reason or "")


def is_owner(chat_id: int | None) -> bool:
    return OWNER_CHAT_ID is not None and chat_id is not None and int(chat_id) == int(OWNER_CHAT_ID)


async def classify(chat_id: int) -> AuthResult:
    """Return the gate decision. Use for fine-grained 403/503 wording.

    Owner > admin-only-mode > approved-group > banned > pending > unknown > allow.

    Group chats (negative chat_ids) go through approved_groups instead of the
    per-user users table. Owner-DM bypasses both. Admin-only mode blocks
    everything except owner."""
    if is_owner(chat_id):
        return "allow"
    mode = await get_admin_only_mode()
    if mode["enabled"]:
        return "deny_admin_only"
    if int(chat_id) < 0:
        # Group / supergroup. Approved → allow whole group; else deny.
        if await _db.is_group_approved(int(chat_id)):
            return "allow"
        return "deny_unknown"
    user = await _db.get_user(int(chat_id))
    if user is None:
        return "deny_unknown"
    status = (user.get("status") or "active").lower()
    if status == "banned":
        return "deny_banned"
    if status == "pending":
        return "deny_pending"
    return "allow"


async def is_authorized(chat_id: int) -> bool:
    """Yes-or-no convenience wrapper around classify()."""
    return (await classify(int(chat_id))) == "allow"


# ── Site allowlist / blocklist ───────────────────────────────────────────────


# Sites blocked-by-default on first boot. Owner can flip these back on in
# the Admin tab, but the safe default is to keep them off of non-owners'
# Sites + Watchlist + download surface. The bot itself still works (owner
# bypasses the gate).
DEFAULT_BLOCKED_PLATFORMS = ["Chaturbate", "Stripchat", "BongaCams", "Cam4"]


# Platforms whose NAMES never surface in the public Sites tab, regardless of
# who's looking. Owner still manages them in the Admin tab; the user-facing
# tab stays screenshot-friendly. The auth/download paths still work; this is
# purely a UI redaction.
HIDDEN_FROM_SITES_TAB = {"Chaturbate", "Stripchat", "BongaCams", "Cam4"}


# Platform → category, for grouping the Admin Site list. Category names
# bubble up to the UI as section headers ("Adult", "Mainstream video", etc.)
# so it's obvious what you're toggling. Anything not listed → "Other".
PLATFORM_CATEGORY: dict[str, str] = {
    # Adult cam sites
    "Chaturbate":  "Adult cam",
    "Stripchat":   "Adult cam",
    "BongaCams":   "Adult cam",
    "Cam4":        "Adult cam",
    # Mainstream live streaming
    "Twitch":      "Live streaming",
    "Kick":        "Live streaming",
    "DLive":       "Live streaming",
    "Trovo":       "Live streaming",
    "YouTube":     "Video",
    "Vimeo":       "Video",
    "Rumble":      "Video",
    # Social / short-form
    "Instagram":   "Social",
    "TikTok":      "Social",
    "Twitter/X":   "Social",
    "Facebook":    "Social",
    "Reddit":      "Social",
    # Region-specific (Chinese) — flag because regional ToS + ban exposure
    # is materially different.
    "Bilibili":    "Regional (CN)",
    "Douyu":       "Regional (CN)",
}


async def get_site_blocklist() -> list[str]:
    """Return the list of platform names (display form, e.g. 'Twitch') that
    are blocked for non-owner users. Stored as a JSON list under the
    `site_blocklist` setting key."""
    raw = await _db.get_setting("site_blocklist", "[]")
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


async def set_site_blocklist(platforms: list[str]) -> list[str]:
    """Replace the blocklist atomically. Returns the persisted list."""
    cleaned = sorted({str(p).strip() for p in platforms if str(p).strip()})
    await _db.set_setting("site_blocklist", json.dumps(cleaned))
    return cleaned


async def is_platform_blocked(url_or_platform: str) -> bool:
    """Check whether a URL's platform (or a literal platform name) is on the
    admin blocklist. Owner-bypass is the caller's job — this is a pure check."""
    bl = await get_site_blocklist()
    if not bl:
        return False
    p = url_or_platform
    if "/" in p or "." in p:
        p = _sm.extract_platform(url_or_platform)
    return p in bl


# ── Bot-token rotation drill ─────────────────────────────────────────────────
#
# We never store the bot token itself in the DB. We store its SHA-256 hash
# (pinned by the owner) so that on every boot we can compare the live
# SMDL_BOT_TOKEN env var against what the owner last approved. A mismatch
# = either a deliberate rotation (and the owner forgot to re-pin) or an
# attacker swapped the env. Either way: surface it loudly in the Admin tab.

import hashlib as _hashlib
import os as _os


def _hash_token(tok: str) -> str:
    return _hashlib.sha256((tok or "").encode("utf-8")).hexdigest()


def _live_bot_token() -> str:
    """Same lookup order as miniapp._verify — the canonical 'what we run with'."""
    return (
        _os.environ.get("SMDL_BOT_TOKEN")
        or _os.environ.get("BOT_TOKEN")
        or _os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    )


async def get_token_health() -> dict:
    """Compare the live token's SHA-256 to the pinned hash. Returns:
        {
          live_hash:   '...' (last 12 chars only — full hash is excessive)
          pinned_hash: '...' (or None)
          pinned_at:   ISO timestamp (or None)
          in_sync:     True/False
          status:      'unpinned' | 'in_sync' | 'drift'
        }
    """
    live = _live_bot_token()
    live_h = _hash_token(live) if live else ""
    pinned_h = (await _db.get_setting("bot_token_hash", "")) or ""
    pinned_at = (await _db.get_setting("bot_token_hash_pinned_at", "")) or ""
    if not pinned_h:
        status = "unpinned"
    elif pinned_h == live_h:
        status = "in_sync"
    else:
        status = "drift"
    return {
        "live_hash":   (live_h[-12:] if live_h else None),
        "pinned_hash": (pinned_h[-12:] if pinned_h else None),
        "pinned_at":   (pinned_at or None),
        "in_sync":     (status == "in_sync"),
        "status":      status,
    }


async def pin_current_token() -> dict:
    """Snapshot the current SMDL_BOT_TOKEN's hash + timestamp. Owner runs
    this immediately after a deliberate rotation."""
    from datetime import datetime as _dt, timezone as _tz
    live = _live_bot_token()
    h = _hash_token(live) if live else ""
    now = _dt.now(_tz.utc).isoformat()
    await _db.set_setting("bot_token_hash", h)
    await _db.set_setting("bot_token_hash_pinned_at", now)
    return await get_token_health()


# ── Default site blocklist seeding ───────────────────────────────────────────


async def seed_default_blocklist_if_unset() -> bool:
    """First-boot: if the owner has never configured the blocklist, seed it
    with DEFAULT_BLOCKED_PLATFORMS. A sentinel setting (`site_blocklist_seeded`)
    distinguishes "never touched" from "owner explicitly set empty", so we
    don't keep re-seeding after the owner intentionally cleared it."""
    seeded = (await _db.get_setting("site_blocklist_seeded", "false")).lower() == "true"
    if seeded:
        return False
    await set_site_blocklist(DEFAULT_BLOCKED_PLATFORMS)
    await _db.set_setting("site_blocklist_seeded", "true")
    return True
