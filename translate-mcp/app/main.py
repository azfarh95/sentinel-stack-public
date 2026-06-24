"""Translate MCP server — LibreTranslate wrapper with local primary + public fallback."""

import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import translator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("Translate MCP server starting (primary=%s fallback=%s)",
                translator.LT_BASE_URL, translator.LT_FALLBACK_URL)
    yield
    logger.info("Translate MCP server shutting down")


mcp = FastMCP(
    "Translate",
    lifespan=_lifespan,
    instructions=(
        "Translate text between languages using LibreTranslate. "
        "Local engine preloaded with: en, zh, ru. "
        "Falls back to public endpoint when local is unavailable. "
        "Use source='auto' to detect language automatically."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*", "translate-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", "http://host.docker.internal:*", "http://translate-mcp:*"],
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def translate_text(
    text: str,
    target: str,
    source: str = "auto",
) -> dict:
    """
    Translate text from one language to another.

    text   : text to translate (plain text)
    target : ISO-639-1 target language code (e.g. "en", "zh", "ru")
    source : ISO-639-1 source language code, or "auto" to detect (default)

    Returns: {translated, detected_source, endpoint}
    """
    try:
        return await translator.translate(text, source, target)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def detect_language(text: str) -> dict:
    """
    Detect the language of the given text.

    text : sample text (a sentence or two is enough)

    Returns: {candidates: [{language, confidence}, ...]}
    """
    try:
        results = await translator.detect(text)
        return {"candidates": results}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_languages() -> dict:
    """
    List languages currently available on the LibreTranslate engine.

    Returns: {languages: [{code, name, targets}, ...]}
    """
    try:
        langs = await translator.list_languages()
        return {"languages": langs}
    except Exception as e:
        return {"error": str(e)}


# ── Health ─────────────────────────────────────────────────────────────────────

async def _health(request):
    return JSONResponse({"status": "ok", "service": "translate-mcp"})


# ── Build ASGI app ────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
