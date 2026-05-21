"""
memory-mcp — Persistent memory store for Sentinel.

MCP Tools:
  memory_store    Save a memory with optional tags and source label.
  memory_search   Full-text search across stored memories.
  memory_list     List memories, optionally filtered by tags.
  memory_get      Retrieve a single memory by ID.
  memory_update   Update content or tags of an existing memory.
  memory_delete   Delete a memory by ID.
  memory_stats    Storage statistics.
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from mcp.server.transport_security import TransportSecuritySettings

from . import database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


db.init_db()  # ensure DB exists at startup, before any MCP session connects


@asynccontextmanager
async def _lifespan(server: FastMCP):
    db.init_db()  # idempotent — safe to call again per-session
    logger.info("memory-mcp ready")
    yield


mcp = FastMCP(
    "memory-mcp",
    lifespan=_lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*", "localhost:*", "[::1]:*",
            "host.docker.internal:*", "memory-mcp:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            "http://host.docker.internal:*", "http://memory-mcp:*",
        ],
    ),
)


@mcp.tool()
def memory_store(
    content: str,
    tags: Optional[list[str]] = None,
    source: Optional[str] = None,
) -> dict:
    """Save a memory. Returns the stored memory with its assigned ID.

    Args:
        content: The memory text to store.
        tags:    Optional list of tags for categorisation (e.g. ["preference", "ui"]).
        source:  Optional label for where this came from (e.g. "user", "research", "incident").
    """
    result = db.store(content, tags or [], source)
    logger.info(f"Stored memory id={result['id']}")
    return result


@mcp.tool()
def memory_search(query: str, limit: Optional[int] = 10) -> list[dict]:
    """Full-text search across all stored memories. Returns ranked results.

    Args:
        query: Search terms (supports FTS5 syntax, e.g. "model mismatch" or "prefer* dark").
        limit: Max results to return (default 10).
    """
    results = db.search(query, limit or 10)
    logger.info(f"Search '{query}' → {len(results)} results")
    return results


@mcp.tool()
def memory_list(
    tags: Optional[list[str]] = None,
    limit: Optional[int] = 50,
) -> list[dict]:
    """List stored memories, newest first. Optionally filter by one or more tags.

    Args:
        tags:  If provided, only return memories containing at least one of these tags.
        limit: Max results (default 50).
    """
    return db.list_all(tags or [], limit or 50)


@mcp.tool()
def memory_get(memory_id: int) -> dict:
    """Retrieve a single memory by its numeric ID.

    Args:
        memory_id: The ID returned when the memory was stored.
    """
    result = db.get_one(memory_id)
    if not result:
        return {"error": f"Memory {memory_id} not found"}
    return result


@mcp.tool()
def memory_update(
    memory_id: int,
    content: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    """Update the content or tags of an existing memory. Omit a field to leave it unchanged.

    Args:
        memory_id: ID of the memory to update.
        content:   New content text (optional).
        tags:      New tag list — replaces existing tags entirely (optional).
    """
    result = db.update(memory_id, content, tags)
    if not result:
        return {"error": f"Memory {memory_id} not found"}
    logger.info(f"Updated memory id={memory_id}")
    return result


@mcp.tool()
def memory_delete(memory_id: int) -> dict:
    """Delete a memory by ID.

    Args:
        memory_id: ID of the memory to delete.
    """
    ok = db.delete(memory_id)
    if not ok:
        return {"error": f"Memory {memory_id} not found"}
    logger.info(f"Deleted memory id={memory_id}")
    return {"status": "deleted", "id": memory_id}


@mcp.tool()
def memory_stats() -> dict:
    """Return storage statistics: total count, oldest/newest timestamps, DB path."""
    return db.stats()


async def _health(request: Request) -> JSONResponse:
    s = db.stats()
    return JSONResponse({"status": "ok", "memories": s["total_memories"]})


async def _memories_list(request: Request) -> JSONResponse:
    limit = int(request.query_params.get("limit", 20))
    tags = request.query_params.getlist("tag")
    return JSONResponse(db.list_all(tags or [], limit))


async def _memories_stats(request: Request) -> JSONResponse:
    return JSONResponse(db.stats())


app = mcp.streamable_http_app()
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
app.router.routes.insert(1, Route("/memories", _memories_list, methods=["GET"]))
app.router.routes.insert(2, Route("/memories/stats", _memories_stats, methods=["GET"]))
