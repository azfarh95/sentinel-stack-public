"""browser-assistant-mcp (convergence P2) — expose the gated/fenced browser
assistant as MCP tools so Dove/OpenClaw can DELEGATE a whole browsing GOAL
instead of hand-driving Playwright primitives one action per LLM call.

This is a THIN SHIM over the existing :8108 surface (POST /run) — it reuses all
of that surface's hardening (approval gate, wall fence, stuck reaper, rate
guard, telemetry, kill-switch). Nothing here re-implements browser control.

Tools:
  browser_task(goal, mode?, vision?, wall_s?)  → run a goal; returns the result
  browser_status()                              → surface health (enabled/busy/stuck)

Approvals: state-changing actions are owner-approved on Telegram (the surface's
default channel), since an MCP caller (Dove) is headless. The call BLOCKS until
the task finishes (bounded by the wall fence).

Transport: streamable HTTP on :8113 (MCP at /mcp), MetaMCP-reachable via
host.docker.internal. Loopback bind; same token model as the surface.
(8103-8107 are Windows/Hyper-V-reserved; 8112 is registered to difficulty-director
in MetaMCP — so this lives on 8113.)
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("browser-assistant-mcp")

SURFACE = "http://127.0.0.1:8108"


def _load_token() -> str:
    t = os.environ.get("COMET_BRIDGE_TOKEN", "").strip()
    if t:
        return t
    p = Path(__file__).resolve().parent.parent / ".env.local"
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("COMET_BRIDGE_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


_TOKEN = _load_token()


def _surface_call(method: str, path: str, body: dict | None = None, timeout: float = 60.0) -> dict:
    headers = {"Content-Type": "application/json"}
    if _TOKEN:
        headers["X-Comet-Token"] = _TOKEN
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SURFACE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*",
                   "browser-assistant-mcp:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                     "http://host.docker.internal:*", "http://browser-assistant-mcp:*"],
)
mcp = FastMCP("browser-assistant", transport_security=_security)


@mcp.tool()
async def browser_task(goal: str, mode: str = "headless",
                       vision: bool = False, wall_s: int = 360, caller: str = "mcp") -> dict:
    """Delegate a whole multi-step BROWSER GOAL to the autonomous, approval-gated
    local browser agent (navigate, read, extract, fill forms, click — it plans the
    steps itself). USE THIS for higher-level goals like "go to X and find/extract Y"
    or "fill the form on Z" instead of issuing individual click/type primitives.

    For shopping price-comparison, prefer the dedicated shopping_search tool — but
    this agent can also call it. State-changing actions (click/type/submit) are
    OWNER-APPROVED on Telegram before they run; reads proceed automatically. The
    call blocks until the task completes or the wall fence fires.

    Args:
      goal    Plain-English task, e.g. "Go to news.ycombinator.com and list the top 3 titles".
      mode    "headless" (isolated throwaway browser, default) | "comet" (attach the
              owner's live Comet via CDP — only if it's running with remote debugging).
      vision  Screenshots on (slower; only helps DOM-blind pages). Default off.
      wall_s  Wall-clock fence in seconds (surface clamps to <=600). Default 360.

    Returns: {status: ok|fenced_timeout|error, final: <result text>, steps, dur_s, ...}.
    """
    try:
        rec = _surface_call("POST", "/run",
                            {"task": goal, "mode": mode, "vision": bool(vision),
                             "channel": "telegram", "wall": int(wall_s),
                             "caller": (caller or "mcp")},
                            timeout=float(wall_s) + 60.0)
        return rec
    except Exception as e:  # noqa: BLE001
        logger.exception("browser_task failed")
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}",
                "hint": "is the :8108 surface up and browser mode enabled (mode.py status)?"}


@mcp.tool()
async def browser_status() -> dict:
    """Health of the browser-assistant surface: enabled (kill-switch), busy, stuck,
    current task, uptime. Check before delegating if you want to avoid a 409 (busy)."""
    try:
        return _surface_call("GET", "/health", timeout=10.0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    logger.info("browser-assistant-mcp starting on :8113 (MCP at /mcp; shim over :8108)")
    uvicorn.run(app, host="127.0.0.1", port=8113, log_level="info")
