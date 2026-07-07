"""Docs MCP server — search + read the Sentinel docs from the LOCAL source.

docs.your-domain.example.com itself is Cloudflare-Pages-hosted (no local port), but its
markdown SOURCE lives in the local pillar repos — which are mounted read-only
under /docs/<pillar>. This server gives the local LLM live, credential-free
access to that source: an SQLite FTS5 index (rebuilt automatically when files
change) plus capped page reads. Fresher than the deployed site (no push/cron
lag), and no CF Access secrets anywhere near the container.

Mirrors the pdf-mcp FastMCP pattern (streamable_http_app + /health) so the
MetaMCP aggregator consumes it exactly like the other *-mcp services.
"""

import logging
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DOCS_ROOT = "/docs"
# Caps tuned for a small-context local LLM (qwen): pages are paginated, search
# snippets are short. Truncation is always surfaced, never silent.
PAGE_CHARS = 16_000
SNIPPET_TOKENS = 32
MAX_RESULTS = 20


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("Docs MCP server starting (root=%s)", DOCS_ROOT)
    yield
    logger.info("Docs MCP server shutting down")


mcp = FastMCP(
    "Docs",
    lifespan=_lifespan,
    instructions=(
        "Search and read the Sentinel Suite documentation (the source behind "
        "docs.your-domain.example.com): pillar overviews, ADRs, runbooks, reference "
        "pages, backlog. Workflow: docs_search to find relevant pages, then "
        "docs_page to read one. docs_toc lists everything; docs_recent shows "
        "what changed lately. Paths are relative like 'core/pillars/defi.md'."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "docs-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*", "http://docs-mcp:*"],
    ),
)


# ── Index ───────────────────────────────────────────────────────────────────────
# In-memory FTS5 index over every *.md under /docs. A cheap fingerprint
# (file count + max mtime + total bytes) is checked on each query; the index
# rebuilds only when the tree actually changed. ~140 small files => rebuilds
# are milliseconds, so correctness wins over cleverness.

_lock = threading.Lock()
_db: sqlite3.Connection | None = None
_fingerprint: tuple | None = None


def _walk_md():
    for base, _dirs, files in os.walk(DOCS_ROOT):
        for f in sorted(files):
            if f.lower().endswith(".md"):
                yield os.path.join(base, f)


def _tree_fingerprint() -> tuple:
    count, max_mtime, total = 0, 0.0, 0
    for p in _walk_md():
        try:
            st = os.stat(p)
        except OSError:
            continue
        count += 1
        total += st.st_size
        max_mtime = max(max_mtime, st.st_mtime)
    return (count, max_mtime, total)


def _title_of(text: str, relpath: str) -> str:
    for line in text.splitlines():
        m = re.match(r"\s*#\s+(.+)", line)
        if m:
            return m.group(1).strip()
    return os.path.splitext(os.path.basename(relpath))[0].replace("-", " ").replace("_", " ")


def _build_index() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute(
        "CREATE VIRTUAL TABLE pages USING fts5"
        "(path UNINDEXED, title, body, mtime UNINDEXED, tokenize='porter unicode61')"
    )
    n = 0
    for p in _walk_md():
        rel = os.path.relpath(p, DOCS_ROOT).replace(os.sep, "/")
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            mtime = os.stat(p).st_mtime
        except OSError as e:
            logger.warning("skipping %s: %s", rel, e)
            continue
        db.execute("INSERT INTO pages VALUES (?,?,?,?)",
                   (rel, _title_of(text, rel), text, mtime))
        n += 1
    db.commit()
    logger.info("indexed %d markdown pages", n)
    return db


def _index() -> sqlite3.Connection:
    global _db, _fingerprint
    with _lock:
        fp = _tree_fingerprint()
        if _db is None or fp != _fingerprint:
            if _db is not None:
                _db.close()
            _db = _build_index()
            _fingerprint = fp
        return _db


def _fts_query(q: str) -> str:
    """User text -> safe FTS5 MATCH expression (quoted terms, implicit AND)."""
    terms = re.findall(r"[A-Za-z0-9_]+", q)
    return " ".join(f'"{t}"' for t in terms)


def _resolve(path: str) -> str:
    """Relative docs path -> absolute path under /docs, traversal-safe."""
    rel = (path or "").strip().lstrip("/").replace("\\", "/")
    if not rel:
        raise ValueError("path is required, e.g. 'core/pillars/defi.md'")
    cand = os.path.realpath(os.path.join(DOCS_ROOT, rel))
    if not (cand + os.sep).startswith(os.path.realpath(DOCS_ROOT) + os.sep) and \
            cand != os.path.realpath(DOCS_ROOT):
        raise ValueError(f"path escapes the docs root: {path}")
    if not os.path.isfile(cand):
        raise ValueError(f"page not found: {rel} (use docs_toc or docs_search)")
    return cand


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def docs_search(query: str, limit: int = 6) -> dict:
    """
    Full-text search across all Sentinel documentation pages (FTS5, porter
    stemming). Returns ranked pages with a short snippet around the match.
    Follow up with docs_page(path) to read a result.

    query : words to search for (plain words work best, e.g. "funding rate risk").
    limit : max results, default 6 (cap 20).
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    limit = max(1, min(int(limit), MAX_RESULTS))
    db = _index()
    match = _fts_query(q)
    if not match:
        return {"error": "query contained no searchable words"}
    rows = db.execute(
        "SELECT path, title, snippet(pages, 2, '>>', '<<', ' … ', ?) "
        "FROM pages WHERE pages MATCH ? ORDER BY rank LIMIT ?",
        (SNIPPET_TOKENS, match, limit),
    ).fetchall()
    if not rows:  # AND found nothing — retry as OR for partial matches
        terms = match.split()
        if len(terms) > 1:
            rows = db.execute(
                "SELECT path, title, snippet(pages, 2, '>>', '<<', ' … ', ?) "
                "FROM pages WHERE pages MATCH ? ORDER BY rank LIMIT ?",
                (SNIPPET_TOKENS, " OR ".join(terms), limit),
            ).fetchall()
    return {
        "query": q,
        "results": [{"path": p, "title": t, "snippet": s} for p, t, s in rows],
        "count": len(rows),
        "hint": "" if rows else "no matches — try fewer or different words",
    }


@mcp.tool()
async def docs_page(path: str, offset: int = 0) -> dict:
    """
    Read one documentation page as Markdown. Long pages are paginated:
    if truncated=true, call again with offset=next_offset for the rest.

    path   : relative path from docs_search/docs_toc, e.g. "core/pillars/defi.md".
    offset : character offset to start from (default 0).
    """
    try:
        abs_path = _resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    with open(abs_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    offset = max(0, int(offset))
    chunk = text[offset:offset + PAGE_CHARS]
    truncated = offset + len(chunk) < len(text)
    return {
        "path": path.strip().lstrip("/"),
        "title": _title_of(text, path),
        "markdown": chunk,
        "offset": offset,
        "next_offset": offset + len(chunk) if truncated else None,
        "total_chars": len(text),
        "truncated": truncated,
    }


@mcp.tool()
async def docs_toc() -> dict:
    """
    List every documentation page (path + title), grouped by top-level section
    (core = the main docs site: pillars/ADRs/runbooks/reference; finance,
    watchdog, smdl, … = per-repo docs). Use docs_search when you know what
    you're looking for — this is for orientation.
    """
    db = _index()
    rows = db.execute("SELECT path, title FROM pages ORDER BY path").fetchall()
    sections: dict[str, list] = {}
    for p, t in rows:
        sections.setdefault(p.split("/", 1)[0], []).append({"path": p, "title": t})
    return {"pages": len(rows), "sections": sections}


@mcp.tool()
async def docs_recent(limit: int = 10) -> dict:
    """
    The most recently modified documentation pages — useful for "what changed
    lately?". Returns path + title, newest first.

    limit : max results, default 10 (cap 20).
    """
    limit = max(1, min(int(limit), MAX_RESULTS))
    db = _index()
    rows = db.execute(
        "SELECT path, title FROM pages ORDER BY mtime DESC LIMIT ?", (limit,)
    ).fetchall()
    return {"results": [{"path": p, "title": t} for p, t in rows]}


# ── Health ─────────────────────────────────────────────────────────────────────

async def _health(request):
    try:
        count = _tree_fingerprint()[0]
    except Exception:
        count = -1
    return JSONResponse({"status": "ok", "service": "docs-mcp", "pages": count})


# ── Build ASGI app ────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.router.routes.insert(
    0,
    __import__("starlette.routing", fromlist=["Route"]).Route("/health", _health, methods=["GET"]),
)
