"""URL detection — regex patterns for supported video platforms.

Core patterns ship with the public release. Site-specific patterns that
should stay out of the public scope (cam sites, niche platforms, etc.)
live in smdl/app/plugins/*.py and register themselves via the public
register_pattern / register_rewrite functions at bottom of this module.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Mainstream / general-purpose video platforms. yt-dlp supports all of these.
# Cam sites and niche platforms go in plugins/, not here.
_PATTERNS: dict[str, list[str]] = {
    "instagram": [
        r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv|stories)/[\w\.\-]+(?:/\d+)?",
        r"https?://(?:www\.)?instagram\.com/[\w\.]+/?\s*$",
    ],
    "tiktok": [
        r"https?://(?:www\.)?tiktok\.com/@[\w\-\.]+/video/\d+",
        r"https?://(?:vm|vt)\.tiktok\.com/[\w\-]+",
    ],
    "youtube": [
        r"https?://(?:www\.)?youtube\.com/shorts/[\w\-]+",
        r"https?://(?:www\.)?youtube\.com/watch\?v=[\w\-]+",
        r"https?://youtu\.be/[\w\-]+",
    ],
    "twitter": [
        r"https?://(?:www\.)?(?:twitter|x)\.com/\w+/status/\d+",
    ],
    "facebook": [
        r"https?://(?:www\.)?facebook\.com/(?:watch|video)\.php\?v=\d+",
        r"https?://fb\.watch/[\w\-]+",
    ],
    "reddit": [
        r"https?://(?:www\.)?reddit\.com/r/\w+/comments/[\w\-]+",
        r"https?://v\.redd\.it/[\w\-]+",
    ],
    "bilibili": [
        r"https?://(?:www\.)?bilibili\.com/video/[\w\-]+",
        r"https?://b23\.tv/[\w\-]+",
    ],
    "pinterest": [
        r"https?://(?:www\.)?pinterest\.com/pin/\d+",
    ],
    # Live-capable platforms. Broad patterns — yt-dlp resolves live vs VOD
    # via extract_info, so we don't pre-filter.
    "twitch": [
        r"https?://(?:www\.|m\.|clips\.)?twitch\.tv/[\w\-/]+",
    ],
    "kick": [
        r"https?://(?:www\.)?kick\.com/[\w\-/]+",
    ],
}

# Mirror-site rewrite table: when a matched URL should be passed to yt-dlp as
# a different canonical domain. Populated by core (empty) + plugins.
# Format: matched_platform → (canonical_platform, old_host, new_host).
_REWRITES: dict[str, tuple[str, str, str]] = {}

_COMPILED: list[tuple[str, re.Pattern]] = []


def _rebuild_compiled() -> None:
    """Re-materialise the (platform, compiled_regex) list. Called whenever
    register_pattern adds to _PATTERNS so we don't have to recompile on
    every find_video_url call."""
    global _COMPILED
    _COMPILED = [
        (platform, re.compile(pattern, re.IGNORECASE))
        for platform, patterns in _PATTERNS.items()
        for pattern in patterns
    ]


# ── Public registration API for plugins ─────────────────────────────────────
def register_pattern(platform: str, *regexes: str) -> None:
    """Add one or more regex patterns for a platform. Plugin entry-point."""
    bucket = _PATTERNS.setdefault(platform, [])
    for r in regexes:
        if r not in bucket:
            bucket.append(r)
    _rebuild_compiled()


def register_rewrite(matched_platform: str, canonical_platform: str,
                     old_host: str, new_host: str) -> None:
    """Map a mirror domain to its canonical form before yt-dlp sees it.

    e.g. xhamsterlive.com/<room> → stripchat.com/<room> so yt-dlp's
    stripchat extractor handles the URL.
    """
    _REWRITES[matched_platform] = (canonical_platform, old_host, new_host)


# Initial compile of core patterns
_rebuild_compiled()


def find_video_url(text: str) -> tuple[str, str] | None:
    """Return (platform, url) for the first video URL found, or None.

    For mirror sites registered via register_rewrite, the URL is rewritten
    to its canonical form so that yt-dlp's existing extractor matches.
    """
    for platform, regex in _COMPILED:
        m = regex.search(text)
        if m:
            url = m.group(0)
            if platform in _REWRITES:
                new_platform, old_host, new_host = _REWRITES[platform]
                url = url.replace(old_host, new_host)
                platform = new_platform
            return platform, url
    return None


# ── Plugin auto-load ─────────────────────────────────────────────────────────
# Plugins register additional patterns / rewrites on import. Loaded AFTER the
# register_* functions and _rebuild_compiled() are defined so plugins can call
# them. Failures here are logged but don't crash the core.
try:
    from . import plugins  # noqa: F401 — side-effect import
except Exception as e:
    logger.warning("plugin load skipped: %s", e)
