"""comet-sidepanel-mcp — self-introspection tools for the OpenClaw sidepanel.

Exposes a small set of tools that let the agent (running in OpenClaw via
MetaMCP) see and reason about its own sidepanel-bridge plumbing:

  bridge_health           : current bridge :8101 /health snapshot
  bridge_audit_tail       : last N audit-log entries from the bridge
  comet_cdp_status        : is Comet running with --remote-debugging-port=9222
                            AND is the CDP endpoint actually serving?
  playwright_mcp_status   : is the Playwright MCP + IPv4 proxy alive?
  describe_architecture   : prose summary of the comet-sidepanel stack so the
                            agent can answer "how do I drive the browser"
                            type questions without us having to bloat TOOLS.md.

Transport: streamable HTTP on 127.0.0.1:8102 (next free 8xxx after bridge
:8101; 8090-8100 are Hyper-V reserved per feedback_port_conventions).
MetaMCP reaches it via http://host.docker.internal:8102/mcp.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("comet-sidepanel-mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_NO_WINDOW    = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hide PS console on tool calls
BRIDGE_BASE   = "http://127.0.0.1:8101"
CDP_URL       = "http://127.0.0.1:9222/json/version"
PW_MCP_PORT   = 8931
PW_PROXY_PORT = 8932
AUDIT_PATH    = Path(os.path.expandvars(r"%USERPROFILE%\metamcp-local\logs\openclaw_bridge.jsonl"))

mcp = FastMCP(
    "comet-sidepanel",
    host="0.0.0.0",  # MetaMCP-in-Docker reaches us via host.docker.internal
    port=8102,
    streamable_http_path="/mcp",
)


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _http_json(url: str, timeout: float = 2.5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ── tools ────────────────────────────────────────────────────────────────────
@mcp.tool()
def bridge_health() -> dict:
    """Return the OpenClaw sidepanel bridge's /health snapshot.

    The bridge is the Windows-host HTTP shim that the Comet side-panel
    extension talks to. If this returns ok=false, the sidebar will show
    "bridge offline" — the agent itself is still reachable from Telegram.
    """
    data = _http_json(f"{BRIDGE_BASE}/health")
    if data is None:
        return {"ok": False, "reachable": False, "url": f"{BRIDGE_BASE}/health"}
    data["ok"] = True
    data["reachable"] = True
    return data


@mcp.tool()
def bridge_audit_tail(limit: int = 10) -> dict:
    """Return the last `limit` lines of the bridge audit log.

    Each entry is one JSON object with keys like {event, session_id,
    duration_ms, model, usage, reply_chars}. Use this to debug
    "why was my last sidebar message slow?" or "is the user actively
    using the sidebar right now?" type questions.
    """
    limit = max(1, min(limit, 200))
    if not AUDIT_PATH.exists():
        return {"ok": False, "error": "no_audit_log", "path": str(AUDIT_PATH)}
    try:
        with AUDIT_PATH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 64 * 1024)
            f.seek(max(0, size - block))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"ok": False, "error": "read_failed", "detail": str(exc)}

    rows = []
    for line in tail[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_raw": line})
    return {"ok": True, "count": len(rows), "entries": rows, "log_path": str(AUDIT_PATH)}


@mcp.tool()
def comet_cdp_status() -> dict:
    """Check whether Comet is running and exposing its Chrome DevTools Protocol on port 9222.

    Returns:
      running   : at least one comet.exe process exists
      flagged   : at least one comet.exe was launched with --remote-debugging-port=9222
      cdp_up    : http://127.0.0.1:9222/json/version answers
      browser   : the Browser string CDP reports (e.g. "Chrome/147.0.7727.1860")
    Without flagged=True and cdp_up=True, Playwright cannot drive the
    sidebar's active tab. Tell the user to run Launch-Comet-CDP.ps1.
    """
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ps = Get-CimInstance Win32_Process -Filter \"Name='comet.exe'\" "
                "-ErrorAction SilentlyContinue; "
                "$flag = ($ps | Where-Object { $_.CommandLine -match '--remote-debugging-port=9222' } | "
                "Select-Object -First 1).ProcessId; "
                "[pscustomobject]@{ count = (@($ps)).Count; flag_pid = $flag } | ConvertTo-Json -Compress"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=_NO_WINDOW,
    )
    info = {}
    try:
        info = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        info = {"count": 0, "flag_pid": None}

    cdp = _http_json(CDP_URL, timeout=2.0)
    return {
        "running": (info.get("count") or 0) > 0,
        "process_count": info.get("count") or 0,
        "flagged": bool(info.get("flag_pid")),
        "flag_pid": info.get("flag_pid"),
        "cdp_up": bool(cdp),
        "browser": (cdp or {}).get("Browser"),
        "webkit_version": (cdp or {}).get("WebKit-Version"),
    }


@mcp.tool()
def playwright_mcp_status() -> dict:
    """Check whether the Playwright MCP server + IPv4 proxy are alive.

    Playwright MCP runs on :8931 and the IPv4 proxy on :8932. MetaMCP
    connects via the proxy. If both are up but CDP (9222) is not, the
    Playwright tools will load but error on first action.
    """
    return {
        "playwright_mcp_8931": _port_open(PW_MCP_PORT),
        "playwright_proxy_8932": _port_open(PW_PROXY_PORT),
    }


@mcp.tool()
def describe_architecture() -> str:
    """Return a prose description of the comet-sidepanel architecture.

    Use this when the user asks "how does the sidebar work" or "how do you
    control my browser" — saves bloating the agent's bootstrap context.
    """
    return (
        "comet-sidepanel architecture:\n"
        "  • Comet (Chromium 147) runs an unpacked side-panel extension loaded from\n"
        "    C:\\Users\\azfar\\metamcp-local\\comet-sidepanel\\extension\\.\n"
        "  • The side panel is a chat UI. Each Comet window has its own session id\n"
        "    of the form `browser-win-<id>`.\n"
        "  • The side panel POSTs each user message to the bridge at\n"
        "    http://127.0.0.1:8101/chat. The bridge is a Python HTTP shim\n"
        "    (bridge.py) that shells out to\n"
        "      wsl -d Ubuntu-24.04 -- node …/openclaw/dist/index.js \\\n"
        "        agent --session-id <id> --message <msg> --json\n"
        "    and returns the parsed reply.\n"
        "  • The agent that runs (this one) can use the whole MetaMCP toolset.\n"
        "    To act on the user's active Comet tab, call the Playwright tools.\n"
        "    Playwright is CDP-attached to Comet on 127.0.0.1:9222 — but only when\n"
        "    Comet was launched via Launch-Comet-CDP.ps1 (the regular Comet\n"
        "    shortcut is unmodified). If comet_cdp_status() returns flagged=false,\n"
        "    tell the user to launch Comet via the wrapper.\n"
        "  • This MCP server (comet-sidepanel) gives you a way to introspect that\n"
        "    plumbing — bridge_health, bridge_audit_tail, comet_cdp_status,\n"
        "    playwright_mcp_status. None of them mutate state.\n"
    )


if __name__ == "__main__":
    logger.info("comet-sidepanel-mcp starting on 0.0.0.0:8102 /mcp")
    mcp.run(transport="streamable-http")
