import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from . import auth, graph
from .parsers import parse_file

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("onedrive-mcp ready. Authenticated: %s", auth.is_authenticated())
    yield


mcp = FastMCP(
    "onedrive-mcp",
    lifespan=_lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*", "localhost:*", "[::1]:*",
            "host.docker.internal:*", "onedrive-mcp:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            "http://host.docker.internal:*", "http://onedrive-mcp:*",
        ],
    ),
)


def _fmt_item(item: dict) -> str:
    is_folder = "folder" in item
    size = item.get("size", 0)
    modified = item.get("lastModifiedDateTime", "")[:10]
    tag = "[DIR] " if is_folder else "[FILE]"
    size_str = f"  {size:,}B" if not is_folder else ""
    return f"{tag} {item['name']}{size_str}  mod:{modified}  id:{item['id']}"


@mcp.tool()
async def onedrive_list(folder_path: str = "") -> str:
    """List files and folders in OneDrive.

    Args:
        folder_path: Path relative to OneDrive root, e.g. 'Documents/Accounting/2025'. Empty = root.
    """
    items = await graph.list_children(folder_path)
    if not items:
        return "Folder is empty or path not found."
    header = f"OneDrive /{folder_path}" if folder_path else "OneDrive (root)"
    return f"{header} — {len(items)} items\n\n" + "\n".join(_fmt_item(i) for i in items)


@mcp.tool()
async def onedrive_search(query: str, limit: int = 20) -> str:
    """Search for files across OneDrive by name or content keyword.

    Args:
        query: Search terms — filename, keyword, or phrase.
        limit: Maximum results to return (default 20).
    """
    items = await graph.search(query, limit)
    if not items:
        return f"No results for '{query}'."
    return f"Search '{query}' — {len(items)} results\n\n" + "\n".join(_fmt_item(i) for i in items)


@mcp.tool()
async def onedrive_read(item_id: str, max_chars: int = 50000) -> str:
    """Read and extract text content from a OneDrive file.
    Supports: Excel (xlsx), PDF, Word (docx), CSV, TXT, MD, JSON.

    Args:
        item_id: The item ID from onedrive_list or onedrive_search results.
        max_chars: Truncate output at this character limit (default 50000).
    """
    item = await graph.get_item(item_id)
    filename = item.get("name", "")
    mime = item.get("file", {}).get("mimeType", "")
    content = await graph.download_item(item_id)
    text = await parse_file(content, filename, mime)
    note = ""
    if len(text) > max_chars:
        note = f"\n\n[truncated — showing {max_chars:,} of {len(text):,} chars]"
        text = text[:max_chars]
    return f"File: {filename}  ({len(content):,} bytes)\n\n{text}{note}"


@mcp.tool()
async def onedrive_metadata(item_id: str) -> str:
    """Get file/folder metadata without downloading content.

    Args:
        item_id: The item ID from onedrive_list or onedrive_search results.
    """
    item = await graph.get_item(item_id)
    lines = [
        f"Name:     {item.get('name')}",
        f"ID:       {item.get('id')}",
        f"Type:     {'Folder' if 'folder' in item else 'File'}",
        f"Size:     {item.get('size', 0):,} bytes",
        f"MIME:     {item.get('file', {}).get('mimeType', 'n/a')}",
        f"Modified: {item.get('lastModifiedDateTime', '')[:19].replace('T', ' ')}",
        f"Created:  {item.get('createdDateTime', '')[:19].replace('T', ' ')}",
        f"Path:     {item.get('parentReference', {}).get('path', 'n/a')}",
    ]
    return "\n".join(lines)


@mcp.tool()
async def onedrive_recent(limit: int = 10) -> str:
    """List recently modified files in OneDrive.

    Args:
        limit: Number of recent files to return (default 10).
    """
    items = await graph.recent(limit)
    if not items:
        return "No recent files found."
    return f"Recently modified — {len(items)} files\n\n" + "\n".join(_fmt_item(i) for i in items)


# ── Auth + health HTTP routes ──────────────────────────────────────────────────

async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "authenticated": auth.is_authenticated()})


async def _auth_start(request: Request) -> RedirectResponse:
    return RedirectResponse(auth.get_auth_url())


async def _auth_callback(request: Request) -> HTMLResponse:
    error = request.query_params.get("error")
    if error:
        desc = request.query_params.get("error_description", "")
        return HTMLResponse(f"<h2>Auth error: {error}</h2><p>{desc}</p>", status_code=400)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    try:
        await auth.exchange_code(code, state)
        return HTMLResponse(
            "<h2>✅ OneDrive connected!</h2>"
            "<p>Authentication successful. You can close this tab.</p>"
            "<p>Sentinel can now access your OneDrive files.</p>"
        )
    except Exception as e:
        return HTMLResponse(f"<h2>Error: {e}</h2>", status_code=500)


app = mcp.streamable_http_app()
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
app.router.routes.insert(1, Route("/auth", _auth_start, methods=["GET"]))
app.router.routes.insert(2, Route("/oauth/callback", _auth_callback, methods=["GET"]))
