"""Maps MCP server — Google Maps directions and search via Telegram inline buttons."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from . import maps as maps_helper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("Maps MCP server starting")
    yield
    logger.info("Maps MCP server shutting down")


mcp = FastMCP(
    "Maps",
    lifespan=_lifespan,
    instructions=(
        "Send Google Maps directions and search links to Telegram as inline buttons. "
        "Supports Singapore postal codes (6 digits) and place names. "
        "No API key required — uses public Google Maps URLs."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*", "maps-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", "http://host.docker.internal:*", "http://maps-mcp:*"],
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def maps_directions(
    chat_id: str,
    origin: str,
    destination: str,
    mode: str = "transit",
) -> dict:
    """
    Send a Google Maps directions link to a Telegram chat as an inline button.
    Tapping the button opens Google Maps inside Telegram with the route pre-loaded.

    chat_id     : Telegram chat ID from the current message.
    origin      : Starting point — Singapore postal code (e.g. "526497"), address,
                  or place name (e.g. "Bedok MRT").
    destination : End point — same formats as origin.
    mode        : "transit" (default) | "driving" | "walking" | "cycling"

    Singapore postal codes are automatically resolved. No API key required.
    """
    try:
        return await maps_helper.directions(chat_id, origin, destination, mode)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def maps_search(
    chat_id: str,
    query: str,
) -> dict:
    """
    Send a Google Maps search link to a Telegram chat as an inline button.
    Tapping opens Google Maps with the search pre-filled.

    chat_id : Telegram chat ID from the current message.
    query   : Anything you'd type into Google Maps, e.g.:
              "hawker centres near Bedok", "nearest MRT to 526497",
              "petrol stations near me", "IKEA Singapore"
    """
    try:
        return await maps_helper.search(chat_id, query)
    except Exception as e:
        return {"error": str(e)}


# ── Health ─────────────────────────────────────────────────────────────────────

async def _health(request):
    return JSONResponse({"status": "ok", "service": "maps-mcp"})


# ── Build ASGI app ────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.router.routes.insert(0, __import__("starlette.routing", fromlist=["Route"]).Route("/health", _health, methods=["GET"]))
