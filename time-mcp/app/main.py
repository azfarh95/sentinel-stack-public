"""Time MCP server — current time + timezone conversion for the local LLM.

LLMs have no clock; this gives qwen a reliable "now" and tz math so scheduling,
reminders and "what time is it in X" reasoning stop guessing. Mirrors the
maps-mcp FastMCP pattern (streamable_http_app + /health).
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_TZ = os.environ.get("LOCAL_TIMEZONE", "Asia/Kuala_Lumpur")
_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("Time MCP server starting (default tz=%s)", DEFAULT_TZ)
    yield
    logger.info("Time MCP server shutting down")


mcp = FastMCP(
    "Time",
    lifespan=_lifespan,
    instructions=(
        "Authoritative current time and timezone math. Call now() before any "
        "time-relative reasoning (scheduling, 'is it past 5pm', age of an event). "
        f"The default timezone is {DEFAULT_TZ}; pass an IANA name "
        "(e.g. 'America/New_York', 'UTC') to override."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "time-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*", "http://time-mcp:*"],
    ),
)


def _zone(tz: str | None) -> ZoneInfo:
    return ZoneInfo((tz or DEFAULT_TZ).strip())


def _describe(dt: datetime) -> dict:
    return {
        "iso": dt.isoformat(),
        "human": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
        "day_of_week": _DOW[dt.weekday()],
        "timezone": str(dt.tzinfo),
        "utc_offset": dt.strftime("%z"),
        "epoch": int(dt.timestamp()),
    }


@mcp.tool()
async def now(timezone: str = "") -> dict:
    """
    Current date and time. Use this before any time-relative reasoning.

    timezone : optional IANA name (e.g. 'UTC', 'America/New_York'). Empty uses
               the configured default ({LOCAL_TIMEZONE}).
    """
    try:
        return _describe(datetime.now(_zone(timezone)))
    except Exception as e:
        return {"error": f"unknown timezone {timezone!r}: {e}"}


@mcp.tool()
async def convert_time(time: str, from_timezone: str, to_timezone: str) -> dict:
    """
    Convert a time between timezones.

    time          : ISO-8601 or 'YYYY-MM-DD HH:MM[:SS]' (naive — interpreted in
                    from_timezone), or 'HH:MM' for today's date in from_timezone.
    from_timezone : IANA name the input is in.
    to_timezone   : IANA name to convert to.
    """
    try:
        ftz, ttz = _zone(from_timezone), _zone(to_timezone)
    except Exception as e:
        return {"error": f"unknown timezone: {e}"}
    s = (time or "").strip()
    dt = None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(s, fmt)
            if fmt in ("%H:%M:%S", "%H:%M"):
                today = datetime.now(ftz)
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            dt = parsed.replace(tzinfo=ftz)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ftz)
        except ValueError:
            return {"error": f"could not parse time {time!r}"}
    out = dt.astimezone(ttz)
    return {"from": _describe(dt), "to": _describe(out)}


@mcp.tool()
async def list_timezones(filter: str = "") -> dict:
    """
    List available IANA timezone names, optionally filtered by substring
    (case-insensitive), e.g. filter='singapore' or 'new_york'.
    """
    f = (filter or "").strip().lower()
    zones = sorted(z for z in available_timezones() if not f or f in z.lower())
    return {"count": len(zones), "timezones": zones[:200],
            "truncated": len(zones) > 200}


async def _health(request):
    return JSONResponse({"status": "ok", "service": "time-mcp",
                         "default_timezone": DEFAULT_TZ})


app = mcp.streamable_http_app()
app.router.routes.insert(
    0,
    __import__("starlette.routing", fromlist=["Route"]).Route("/health", _health, methods=["GET"]),
)
