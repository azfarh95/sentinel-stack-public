"""URL detection — regex patterns for supported video platforms."""

import re

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
}

_COMPILED = [
    (platform, re.compile(pattern, re.IGNORECASE))
    for platform, patterns in _PATTERNS.items()
    for pattern in patterns
]


def find_video_url(text: str) -> tuple[str, str] | None:
    """Return (platform, url) for the first video URL found, or None."""
    for platform, regex in _COMPILED:
        m = regex.search(text)
        if m:
            return platform, m.group(0)
    return None
