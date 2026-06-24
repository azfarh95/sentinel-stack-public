"""SQLite MCP server — READ-ONLY ad-hoc query access to the Sentinel data
stores (primarily the Finance GL in portfolio.db) for the local LLM.

Safety is layered:
  * the .db files are mounted read-only (compose `:ro`),
  * each connection is opened with `mode=ro` + `PRAGMA query_only=ON`,
  * the query tool accepts a single SELECT/WITH statement only (no DDL/DML,
    no multiple statements),
  * results are row- and byte-capped.

Mirrors the maps-mcp FastMCP pattern (streamable_http_app + /health).
"""

import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DB_DIR = Path(os.environ.get("DB_DIR", "/db"))
DEFAULT_DB = os.environ.get("DEFAULT_DB", "portfolio.db")
MAX_ROWS = 1000
DEFAULT_ROWS = 200
MAX_CELL = 2000   # truncate huge text/blob cells

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|"
    r"ATTACH|DETACH|VACUUM|REINDEX|GRANT|REVOKE|PRAGMA)\b",
    re.IGNORECASE,
)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("SQLite MCP starting (db_dir=%s default=%s)", DB_DIR, DEFAULT_DB)
    yield
    logger.info("SQLite MCP shutting down")


mcp = FastMCP(
    "SQLite",
    lifespan=_lifespan,
    instructions=(
        "READ-ONLY SQL over the Sentinel data stores — primarily the Finance "
        "general ledger in portfolio.db. Workflow: list_databases -> list_tables "
        "-> describe_table -> query. Only SELECT/WITH statements are accepted; "
        "the data cannot be modified. Use this for ad-hoc analytics the "
        "purpose-built finance tools don't expose (sums by month, drift checks, "
        "joins). Results are capped at 1000 rows."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "sqlite-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*", "http://sqlite-mcp:*"],
    ),
)


def _db_path(database: str | None) -> Path:
    """Resolve a database arg to a real .db file under DB_DIR. Basename only —
    no path traversal."""
    name = os.path.basename((database or DEFAULT_DB).strip())
    if not name.endswith((".db", ".sqlite", ".sqlite3")):
        name += ".db"
    p = DB_DIR / name
    if not p.exists():
        raise ValueError(f"database not found: {name}")
    return p


def _connect_ro(p: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _is_safe_select(sql: str):
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return None, "empty query"
    if ";" in s:
        return None, "multiple statements are not allowed"
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return None, "only SELECT / WITH queries are allowed (read-only)"
    if _FORBIDDEN.search(s):
        return None, "write/DDL keywords are not allowed"
    return s, None


def _clip(v):
    if isinstance(v, bytes):
        return f"<blob {len(v)} bytes>"
    if isinstance(v, str) and len(v) > MAX_CELL:
        return v[:MAX_CELL] + "…"
    return v


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_databases() -> dict:
    """List the available SQLite databases (read-only) with their table counts."""
    out = []
    for f in sorted(DB_DIR.glob("*.db")):
        try:
            with _connect_ro(f) as c:
                n = c.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
        except Exception as e:
            n = f"error: {e}"
        out.append({"database": f.name, "tables": n,
                    "default": f.name == DEFAULT_DB,
                    "size_bytes": f.stat().st_size})
    return {"db_dir": str(DB_DIR), "databases": out}


@mcp.tool()
async def list_tables(database: str = "") -> dict:
    """
    List tables (and views) in a database, with row counts.

    database : optional .db name (default portfolio.db — the Finance GL).
    """
    try:
        p = _db_path(database)
    except Exception as e:
        return {"error": str(e)}
    try:
        with _connect_ro(p) as c:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            tables = []
            for t in names:
                try:
                    rc = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    rc = None
                tables.append({"name": t, "rows": rc})
        return {"database": p.name, "count": len(tables), "tables": tables}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def describe_table(table: str, database: str = "") -> dict:
    """
    Column schema + row count for one table.

    table    : table or view name.
    database : optional .db name (default portfolio.db).
    """
    try:
        p = _db_path(database)
    except Exception as e:
        return {"error": str(e)}
    if not re.fullmatch(r"[A-Za-z0-9_]+", table or ""):
        return {"error": "invalid table name"}
    try:
        with _connect_ro(p) as c:
            cols = c.execute(f'PRAGMA table_info("{table}")').fetchall()
            if not cols:
                return {"error": f"table not found: {table}"}
            try:
                rc = c.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            except Exception:
                rc = None
            schema = [{"name": r["name"], "type": r["type"],
                       "notnull": bool(r["notnull"]), "pk": bool(r["pk"])}
                      for r in cols]
        return {"database": p.name, "table": table, "rows": rc, "columns": schema}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def query(sql: str, database: str = "", limit: int = DEFAULT_ROWS) -> dict:
    """
    Run a READ-ONLY SQL query (single SELECT/WITH statement) and return rows.
    The database cannot be modified. Results are capped at 1000 rows.

    sql      : a single SELECT or WITH…SELECT statement.
    database : optional .db name (default portfolio.db — the Finance GL).
    limit    : max rows to return (default 200, hard cap 1000).
    """
    safe, err = _is_safe_select(sql)
    if err:
        return {"error": err}
    try:
        p = _db_path(database)
    except Exception as e:
        return {"error": str(e)}
    lim = max(1, min(int(limit or DEFAULT_ROWS), MAX_ROWS))
    try:
        with _connect_ro(p) as c:
            cur = c.execute(safe)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(lim)
            data = [{k: _clip(r[k]) for k in cols} for r in rows]
            more = cur.fetchone() is not None
        return {"database": p.name, "columns": cols, "row_count": len(data),
                "rows": data, "truncated": more}
    except Exception as e:
        return {"error": f"query failed: {e}"}


async def _health(request):
    dbs = sorted(f.name for f in DB_DIR.glob("*.db")) if DB_DIR.exists() else []
    return JSONResponse({"status": "ok", "service": "sqlite-mcp", "databases": dbs})


app = mcp.streamable_http_app()
app.router.routes.insert(
    0,
    __import__("starlette.routing", fromlist=["Route"]).Route("/health", _health, methods=["GET"]),
)
