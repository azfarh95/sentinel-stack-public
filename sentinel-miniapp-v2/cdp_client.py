"""CDP (Chrome DevTools Protocol) client for the V3.4 high-fps Browser panel.

Connects to a Chromium instance launched with --remote-debugging-port,
subscribes to Page.startScreencast for paint-driven frame delivery, and
exposes Input.dispatchMouseEvent / KeyEvent / dispatchScrollEvent for
real input replay.

Falls back gracefully — bridge can ignore CDP entirely if Chromium isn't
running on the configured port; existing polling loop remains as fallback.

Threading model:
- One persistent thread per active page connection.
- Frames captured into a shared buffer that the SSE writer reads.
- Input dispatch is fire-and-forget from the request thread.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

import websocket


class CDPClient:
    """Connects to a Chromium page via CDP, subscribes to screencast, exposes
    input dispatch. Not thread-safe by itself — caller serialises access."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9222):
        self.host = host
        self.port = port
        self.ws: websocket.WebSocket | None = None
        self.page_id: str | None = None
        self.target_url: str | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.ws is not None

    def discover_page(self) -> str | None:
        """Hit /json on the CDP HTTP port and pick the first 'page' target.
        Returns the WebSocket URL or None if Chromium isn't reachable."""
        try:
            r = urllib.request.urlopen(f"http://{self.host}:{self.port}/json", timeout=2)
            for t in json.loads(r.read()):
                if t.get("type") == "page":
                    self.target_url = t.get("url", "")
                    self.page_id = t.get("id", "")
                    return t.get("webSocketDebuggerUrl")
        except (urllib.error.URLError, OSError):
            return None
        return None

    def connect(self) -> bool:
        page_ws = self.discover_page()
        if not page_ws:
            return False
        try:
            self.ws = websocket.create_connection(page_ws, timeout=8)
            self.ws.settimeout(0.3)
            self._connected = True
            self._send("Page.enable")
            self._inject_stealth()
            return True
        except Exception as e:
            print(f"[cdp] connect failed: {e}")
            self.ws = None
            self._connected = False
            return False

    def _inject_stealth(self):
        """Run a stealth shim on every new document — masks navigator.webdriver,
        chrome.runtime presence, plugins length, and a few other classic
        automation tells. Will defeat naive bot checks but NOT Cloudflare
        Turnstile (which uses mouse-trajectory + timing fingerprints).
        Equivalent in spirit to puppeteer-extra-plugin-stealth core patches."""
        script = r"""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            if (!window.chrome) { window.chrome = { runtime: {} }; }
            Object.defineProperty(navigator, 'plugins',  {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages',{get: () => ['en-US','en']});
            const origQuery = navigator.permissions && navigator.permissions.query;
            if (origQuery) {
                navigator.permissions.query = (p) =>
                    p && p.name === 'notifications'
                        ? Promise.resolve({state: Notification.permission})
                        : origQuery(p);
            }
        """
        self._send("Page.addScriptToEvaluateOnNewDocument", {"source": script})

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None
        self._connected = False

    def _send(self, method: str, params: dict | None = None) -> int:
        if not self.ws:
            return -1
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            msg = {"id": mid, "method": method}
            if params:
                msg["params"] = params
            try:
                self.ws.send(json.dumps(msg))
            except Exception:
                self._connected = False
                return -1
        return mid

    # ── Screencast ──────────────────────────────────────────────────────

    def start_screencast(self, max_width: int = 900, max_height: int = 580,
                          jpeg_quality: int = 55, every_nth_frame: int = 1):
        """Defaults tuned for mobile (~30-50 KB per frame, ~250 Kbps wire).
        v3.4.1 dropped from 1280x800 q70 (~140 KB) → 900x580 q55 (~40 KB)
        for ~3.5x bandwidth reduction without visible quality loss on phone."""
        self._send("Page.startScreencast", {
            "format": "jpeg",
            "quality": jpeg_quality,
            "maxWidth": max_width,
            "maxHeight": max_height,
            "everyNthFrame": every_nth_frame,
        })

    def stop_screencast(self):
        self._send("Page.stopScreencast")

    def ack_frame(self, session_id: int):
        self._send("Page.screencastFrameAck", {"sessionId": session_id})

    def set_cookies(self, cookies: list[dict]) -> int:
        """Bulk-set cookies via Network.setCookies. Each cookie dict needs at
        minimum {name, value, domain or url}. Returns count of cookies sent.
        Uses Network.setCookies (plural) which accepts a list in one call."""
        if not cookies:
            return 0
        rid = self._send("Network.setCookies", {"cookies": cookies})
        return len(cookies) if rid > 0 else 0

    def capture_screenshot(self, jpeg_quality: int = 55, max_width: int = 900) -> str | None:
        """One-shot screenshot via Page.captureScreenshot — for keep-alive frames
        when screencast goes silent (static pages) or during page load when paint
        events are sparse. Synchronous; blocks up to 5s for the response."""
        rid = self._send("Page.captureScreenshot", {
            "format": "jpeg", "quality": jpeg_quality,
            "captureBeyondViewport": False,
        })
        if rid < 0:
            return None
        # Drain until we see the response to our request id (collect screencast
        # frames meanwhile so they aren't lost — caller's job to read them via
        # receive_one in normal flow; here we just discard non-matching)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            self.ws.settimeout(max(0.05, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except Exception:
                return None
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == rid:
                return msg.get("result", {}).get("data") or None
        return None

    def receive_one(self, timeout: float = 0.3) -> dict | None:
        """Read one CDP message; returns None on timeout/error."""
        if not self.ws:
            return None
        self.ws.settimeout(timeout)
        try:
            raw = self.ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except Exception:
            self._connected = False
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    # ── Input dispatch ──────────────────────────────────────────────────

    def click(self, x: int, y: int, button: str = "left", click_count: int = 1):
        # Brief mouse-move trajectory before the click — 4 intermediate points
        # over ~80ms. Defeats "instant click" heuristics in basic bot checks.
        # Doesn't help against trajectory-fingerprinting (Cloudflare Turnstile).
        try:
            steps = 4
            x0, y0 = int(x) - 40, int(y) - 25  # arbitrary start offset
            for i in range(1, steps + 1):
                ix = x0 + (int(x) - x0) * i // steps
                iy = y0 + (int(y) - y0) * i // steps
                self._send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": ix, "y": iy,
                })
                time.sleep(0.018)
        except Exception:
            pass
        for kind in ("mousePressed", "mouseReleased"):
            self._send("Input.dispatchMouseEvent", {
                "type": kind, "x": int(x), "y": int(y),
                "button": button, "clickCount": click_count,
            })

    def mouse_move(self, x: int, y: int):
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(x), "y": int(y)})

    def scroll(self, x: int, y: int, delta_y: int):
        self._send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": int(x), "y": int(y),
            "deltaX": 0, "deltaY": int(delta_y),
        })

    def type_text(self, text: str):
        for ch in text:
            self._send("Input.dispatchKeyEvent", {"type": "char", "text": ch})

    def key(self, key_name: str):
        """Send a special key. key_name like 'Enter', 'Escape', 'Tab', 'Backspace'."""
        special = {
            "Enter":     {"key": "Enter",     "code": "Enter",     "windowsVirtualKeyCode": 13},
            "Escape":    {"key": "Escape",    "code": "Escape",    "windowsVirtualKeyCode": 27},
            "Tab":       {"key": "Tab",       "code": "Tab",       "windowsVirtualKeyCode": 9},
            "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
            "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
            "ArrowUp":   {"key": "ArrowUp",   "code": "ArrowUp",   "windowsVirtualKeyCode": 38},
        }
        info = special.get(key_name)
        if not info:
            return
        for kind in ("keyDown", "keyUp"):
            self._send("Input.dispatchKeyEvent", {"type": kind, **info})


def background_screencast_loop(
    client: CDPClient,
    on_frame: Callable[[str, str], None],   # (jpeg_b64, page_url) -> None
    should_run: Callable[[], bool],         # returns False to stop
    on_disconnect: Callable[[], None] | None = None,
):
    """Run on a daemon thread. Reconnects automatically if Chromium goes
    away, screencasts only when should_run() returns True (i.e. SSE clients
    connected)."""
    last_run = False
    while True:
        if not should_run():
            if last_run and client.connected:
                client.stop_screencast()
                last_run = False
            time.sleep(0.5)
            continue
        if not client.connected:
            if not client.connect():
                time.sleep(2.0)
                continue
            last_run = False  # need to start screencast on fresh connection
        if not last_run:
            client.start_screencast()
            last_run = True
        msg = client.receive_one(timeout=0.3)
        if msg is None:
            if not client.connected and on_disconnect:
                on_disconnect()
            continue
        if msg.get("method") == "Page.screencastFrame":
            params = msg["params"]
            jpeg = params.get("data", "")
            page_url = ""
            try:
                page_url = client.target_url or ""
            except Exception:
                pass
            on_frame(jpeg, page_url)
            client.ack_frame(params["sessionId"])
