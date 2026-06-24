"""Playwright MCP client for the Sentinel mini app browser panel.

Calls Playwright tools through MetaMCP at localhost:12008, which routes to the
same Playwright session OpenClaw uses — so screenshots show the agent's actual
browser state, not a separate context.

Usage:
    client = PlaywrightMCPClient(token="sk_mt_...")
    jpeg_b64 = client.screenshot()       # current page as JPEG base64
    info = client.snapshot()             # accessibility snapshot (URL, title)
    client.close()
"""
import json
import threading
import time
import urllib.error
import urllib.request

METAMCP_URL = "http://localhost:12008/metamcp/default/mcp"


def _parse_sse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except Exception:
                pass
    return {}


class PlaywrightMCPClient:
    """Thread-safe MCP client. Lazy session init, auto-reconnect on errors."""

    def __init__(self, token: str, url: str = METAMCP_URL, timeout: float = 30.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self._session_id: str | None = None
        self._lock = threading.Lock()
        self._tool_name = "Playwright__browser_take_screenshot"

    def _headers(self, sid: str | None = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if sid:
            h["mcp-session-id"] = sid
        return h

    def _open_session(self) -> str:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sentinel-bridge-v2-browser", "version": "0.1"},
            },
        }).encode()
        req = urllib.request.Request(self.url, data=body, headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            sid = r.headers.get("mcp-session-id", "")
            r.read()
        if not sid:
            raise RuntimeError("MetaMCP returned no session id")
        # Required: send initialized notification
        notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(self.url, data=notif, headers=self._headers(sid)),
                timeout=5,
            ).read()
        except Exception:
            pass  # notifications may return 202/empty; non-fatal
        return sid

    def _ensure_session(self) -> str:
        with self._lock:
            if self._session_id:
                return self._session_id
            self._session_id = self._open_session()
            return self._session_id

    def _drop_session(self):
        with self._lock:
            self._session_id = None

    def _force_release_session(self):
        """Close current MCP session so the agent can use the browser between
        bridge captures. Sends DELETE if available, else just drops the id."""
        sid = self._session_id
        if not sid:
            return
        try:
            req = urllib.request.Request(self.url, method="DELETE",
                                          headers=self._headers(sid))
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass
        self._drop_session()

    def _call(self, tool: str, arguments: dict | None = None, retry: bool = True) -> dict:
        sid = self._ensure_session()
        body = json.dumps({
            "jsonrpc": "2.0", "id": int(time.time() * 1000) % 100000,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }).encode()
        req = urllib.request.Request(self.url, data=body, headers=self._headers(sid))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
            return _parse_sse(raw)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 404) and retry:
                # Session expired; reopen and retry once
                self._drop_session()
                return self._call(tool, arguments, retry=False)
            raise

    def screenshot(self, jpeg_quality: int = 70, release: bool = True) -> str | None:
        """Return base64-encoded JPEG of the current Playwright page, or None on error.
        If release=True, the MCP session is released after the capture so the agent
        can use the browser between captures (avoids contention)."""
        try:
            result = self._call(self._tool_name, {"type": "jpeg"})
        except Exception:
            self._force_release_session()
            return None
        jpeg = None
        for item in result.get("result", {}).get("content", []):
            if item.get("type") == "image":
                jpeg = item.get("data") or None
                break
        if release:
            self._force_release_session()
        return jpeg

    def page_info(self) -> dict:
        """Return basic page metadata (URL, title) by calling browser_snapshot.
        Cheaper than a full screenshot; suitable for the status badge."""
        try:
            result = self._call("Playwright__browser_snapshot", {})
        except Exception:
            return {}
        for item in result.get("result", {}).get("content", []):
            if item.get("type") == "text":
                text = item.get("text", "")
                # snapshot text starts with metadata like "url: <url>\ntitle: <t>"
                meta = {}
                for ln in text.splitlines()[:10]:
                    if ":" in ln:
                        k, _, v = ln.partition(":")
                        meta[k.strip().lower()] = v.strip()
                return {"url": meta.get("url", ""), "title": meta.get("title", "")}
        return {}

    def close(self):
        """Best-effort session close. Not critical — sessions time out naturally."""
        self._drop_session()
