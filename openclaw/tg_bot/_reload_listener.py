"""#42 — Loopback HTTP listener for /internal/reload-env in the brain bot.

The shared-brain Telegram bot is a polling client (no web framework), so we
spin up a tiny stdlib http.server thread to expose the #27 fanout endpoint.
Bound to 127.0.0.1:8108. Token-gated via INTERNAL_RELOAD_TOKEN, same as
every other consumer.

The brain only reads one rotation-worthy key from .env.local —
LLM_API_KEY — and it does so via a dynamic os.environ.get() lookup
each call (with a negative cache fallback). Pushing the new value into
os.environ is sufficient; we register a hot-swap callback that invalidates
the negative cache so the next call re-reads.
"""
from __future__ import annotations

import hmac
import http.server
import json
import logging
import os
import socketserver
import threading
from pathlib import Path

from openclaw.tg_bot import _reload_env as _renv

logger = logging.getLogger("openclaw.tg_bot.reload")

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8108


def _swap_lm_api_key(v: str) -> None:
    """Invalidate the brain bot's negative cache for LLM_API_KEY so the
    next call to _lm_studio_key() re-resolves from os.environ (which the
    reload library just pushed the new value into)."""
    from openclaw.tg_bot import attachment_processor as ap
    ap._CACHED_LM_KEY = None


_renv.register_hot_swap("LLM_API_KEY", _swap_lm_api_key)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        logger.info("reload-listener %s — %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/internal/reload-env":
            self._send(404, {"detail": "not found"})
            return
        host = (self.client_address or ("", 0))[0] or ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            self._send(403, {"detail": f"internal endpoint: loopback only (got {host})"})
            return
        expected = os.environ.get("INTERNAL_RELOAD_TOKEN", "")
        presented = self.headers.get("X-Internal-Reload-Token", "")
        if not expected:
            self._send(503, {"detail": "INTERNAL_RELOAD_TOKEN not set in env"})
            return
        if not hmac.compare_digest(expected, presented):
            self._send(401, {"detail": "internal endpoint: token mismatch"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        keys = body.get("keys") if isinstance(body, dict) else None
        env_path_str = os.environ.get(
            "ENV_LOCAL_PATH", r"C:\Users\azfar\metamcp-local\.env.local",
        )
        result = _renv.reload_env_in_process(Path(env_path_str), keys=keys)
        self._send(200, {"ok": True, **result})


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_reload_listener() -> None:
    """Spawn the listener in a daemon thread. Idempotent; logs + skips
    if the port is already in use (another instance is already serving)."""
    def _serve():
        try:
            with _ThreadingServer((LISTEN_HOST, LISTEN_PORT), _Handler) as srv:
                logger.info("reload-env listener on http://%s:%s", LISTEN_HOST, LISTEN_PORT)
                srv.serve_forever()
        except OSError as exc:
            logger.warning("reload-env listener failed to bind %s:%s: %s",
                           LISTEN_HOST, LISTEN_PORT, exc)

    threading.Thread(target=_serve, daemon=True, name="reload-env-listener").start()
