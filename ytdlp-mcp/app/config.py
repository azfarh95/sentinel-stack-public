"""Live-recording config for SMDL MCP.

In the standalone SMDL bot, these come from /config/smdl.json. SMDL MCP
is invoked by an LLM agent which can pass per-call overrides — so this
file only needs sensible defaults. Env vars take precedence so an
operator can tune without rebuilding the image.
"""
import os

# Mirrors smdl/app/config.py — see that file for full rationale per key.
LIVE_ABORT_ON_SESSION_FAIL    = os.environ.get("LIVE_ABORT_ON_SESSION_FAIL", "true").lower() == "true"
LIVE_HEARTBEAT_SECONDS        = int(os.environ.get("LIVE_HEARTBEAT_SECONDS", "300"))
LIVE_MAX_CONCURRENT           = int(os.environ.get("LIVE_MAX_CONCURRENT", "1"))
LIVE_MAX_HEIGHT               = int(os.environ.get("LIVE_MAX_HEIGHT", "720"))
LIVE_MIN_FREE_DISK_GB         = int(os.environ.get("LIVE_MIN_FREE_DISK_GB", "10"))
LIVE_PLATFORMS                = {
    p.strip().lower() for p in
    os.environ.get("LIVE_PLATFORMS", "youtube,twitch,kick").split(",")
}
LIVE_TRANSCODE_HEIGHT         = int(os.environ.get("LIVE_TRANSCODE_HEIGHT", "0"))
LIVE_TRANSCODE_KEEP_ORIGINAL  = os.environ.get("LIVE_TRANSCODE_KEEP_ORIGINAL", "false").lower() == "true"
