"""Central configuration — env-derived constants in one place.

Replaces direct `os.environ.get(...)` reads scattered across business logic.
Read once at import; use the module attributes everywhere else.

Future: turn into a Config dataclass + validation at startup, accept overrides
from a YAML for multi-tenant deployments.
"""
import os


# Default Web3 wallet — the on-chain polling job needs this; agent endpoints
# and dashboard fall back to this when no address arg is supplied.
DEFAULT_ADDR: str = os.environ.get("PORTFOLIO_DEFAULT_ADDRESS", "")

# Snapshot dust threshold — positions worth less than this in USD are filtered
# out of portfolio snapshots so the GL stays clean.
DUST: float = float(os.environ.get("PORTFOLIO_DUST_USD", "0.01"))

# How often the on-chain wallet poller runs (minutes).
POLL_INTERVAL_MIN: int = int(os.environ.get("ONCHAIN_POLL_INTERVAL_MIN", "5"))

# Telegram bot listener — disabled by default because polling getUpdates with
# python-telegram-bot v21 races against MCP-session lifespan reinitialization.
# Outbound `notifier.send()` works regardless.
BOT_LISTENER_ENABLED: bool = os.environ.get("BOT_LISTENER_ENABLED", "0") == "1"

# Wise sync — only schedules if a token is configured.
WISE_API_TOKEN: str = os.environ.get("WISE_API_TOKEN", "")

# Firefly internal URL (legacy — being decoupled, but some bridge code paths
# still reach in to void historical entries).
FIREFLY_INTERNAL_URL: str = os.environ.get(
    "FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180"
)

# PWA host override for generated manifest.webmanifest links.
PWA_HOST_OVERRIDE: str = os.environ.get("PWA_HOST_OVERRIDE", "")
