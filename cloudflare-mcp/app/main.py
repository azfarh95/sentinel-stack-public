"""Cloudflare MCP server — READ-ONLY visibility into the Cloudflare estate for
the local LLM: zones, DNS, Tunnels, Pages, Access (Zero Trust).

Uses a scoped, read-only API token (CLOUDFLARE_API_TOKEN). No write tools are
exposed, so qwen can observe but never mutate DNS / tunnels / etc. Mirrors the
maps-mcp FastMCP pattern (streamable_http_app + /health).
"""

import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
BASE = "https://api.cloudflare.com/client/v4"
_account_id: str | None = None


@asynccontextmanager
async def _lifespan(server: FastMCP):
    logger.info("Cloudflare MCP starting (token present=%s)", bool(TOKEN))
    yield
    logger.info("Cloudflare MCP shutting down")


mcp = FastMCP(
    "Cloudflare",
    lifespan=_lifespan,
    instructions=(
        "READ-ONLY view of the Cloudflare account: zones (domains), DNS records, "
        "Cloudflare Tunnels, Pages projects, and Access (Zero Trust) apps. Use it "
        "to answer 'what DNS points at X', 'is the tunnel up', 'what Pages deploys "
        "exist', 'which Access apps gate which host'. Cannot change anything."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*",
                       "host.docker.internal:*", "cloudflare-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         "http://host.docker.internal:*", "http://cloudflare-mcp:*"],
    ),
)


async def _get(path: str, params: dict | None = None) -> dict:
    if not TOKEN:
        raise RuntimeError("CLOUDFLARE_API_TOKEN not set")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(BASE + path, headers={"Authorization": f"Bearer {TOKEN}"}, params=params)
    data = r.json()
    if not data.get("success", False):
        raise RuntimeError(str(data.get("errors") or data))
    return data


async def _account() -> str:
    global _account_id
    if _account_id is None:
        d = await _get("/accounts")
        if not d["result"]:
            raise RuntimeError("token can't see any account")
        _account_id = d["result"][0]["id"]
    return _account_id


async def _zone_id(zone: str) -> str:
    z = (zone or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", z):
        return z
    d = await _get("/zones", {"name": z})
    if not d["result"]:
        raise RuntimeError(f"zone not found: {zone}")
    return d["result"][0]["id"]


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def verify_token() -> dict:
    """Check the Cloudflare API token is active. Good first call to confirm auth."""
    try:
        d = await _get("/user/tokens/verify")
        return {"status": d["result"].get("status"), "token_id": d["result"].get("id")}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_zones() -> dict:
    """List Cloudflare zones (domains) with status and plan."""
    try:
        d = await _get("/zones", {"per_page": 50})
        return {"zones": [{"name": z["name"], "id": z["id"], "status": z["status"],
                           "plan": (z.get("plan") or {}).get("name")} for z in d["result"]]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_dns_records(zone: str, type: str = "", name: str = "") -> dict:
    """
    DNS records for a zone.

    zone : zone name (e.g. 'your-domain.example.com') or its 32-char id.
    type : optional filter, e.g. 'A', 'CNAME', 'TXT'.
    name : optional exact record name filter (e.g. 'docs.your-domain.example.com').
    """
    try:
        zid = await _zone_id(zone)
        params: dict = {"per_page": 300}
        if type:
            params["type"] = type.upper()
        if name:
            params["name"] = name
        d = await _get(f"/zones/{zid}/dns_records", params)
        return {"zone": zone, "count": len(d["result"]),
                "records": [{"name": r["name"], "type": r["type"], "content": r["content"],
                             "proxied": r.get("proxied"), "ttl": r.get("ttl")}
                            for r in d["result"]]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_tunnels() -> dict:
    """List Cloudflare Tunnels (cloudflared) on the account with their status."""
    try:
        aid = await _account()
        d = await _get(f"/accounts/{aid}/cfd_tunnel", {"per_page": 100, "is_deleted": "false"})
        return {"tunnels": [{"name": t["name"], "id": t["id"], "status": t.get("status"),
                             "connections": len(t.get("connections") or []),
                             "created": t.get("created_at")} for t in d["result"]]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_pages_projects() -> dict:
    """List Cloudflare Pages projects with their subdomain + latest deployment."""
    try:
        aid = await _account()
        d = await _get(f"/accounts/{aid}/pages/projects")
        out = []
        for p in d["result"]:
            ld = p.get("latest_deployment") or {}
            out.append({"name": p["name"], "subdomain": p.get("subdomain"),
                        "domains": p.get("domains"),
                        "latest_deploy": {"env": ld.get("environment"),
                                          "created": ld.get("created_on"),
                                          "url": ld.get("url")}})
        return {"projects": out}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_access_apps() -> dict:
    """List Cloudflare Access (Zero Trust) applications and the hosts they gate."""
    try:
        aid = await _account()
        d = await _get(f"/accounts/{aid}/access/apps")
        return {"apps": [{"name": a.get("name"), "domain": a.get("domain"),
                          "type": a.get("type"), "id": a.get("id")} for a in d["result"]]}
    except Exception as e:
        return {"error": str(e)}


async def _health(request):
    return JSONResponse({"status": "ok", "service": "cloudflare-mcp",
                         "token_present": bool(TOKEN)})


app = mcp.streamable_http_app()
app.router.routes.insert(
    0,
    __import__("starlette.routing", fromlist=["Route"]).Route("/health", _health, methods=["GET"]),
)
