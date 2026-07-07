"""
Comet Sidepanel Bridge — HTTP/SSE bridge between the Comet side-panel
extension and the OpenClaw gateway in WSL2.

Endpoints
---------
GET  /health          → {"ok": true, "uptime_s": <int>}
POST /chat            → {"message": str, "session_id": str?}  (synchronous)
                        returns {"reply": str, "session_id": str,
                                 "runId": str, "duration_ms": int,
                                 "model": str, "usage": {...}}
GET  /events?session= → SSE stream of {event: "token"|"tool"|"done", data: …}
                        (v2 — not implemented yet; placeholder 501)

Architecture
------------
The bridge is a thin shim. Each /chat call:
  1. Validates session_id (alnum + dashes only — OpenClaw rejects colons).
  2. Spawns `wsl -d Ubuntu-24.04 -- bash -lc 'node …/openclaw agent …'`.
  3. Parses the JSON output, extracts the reply text + meta.
  4. Returns it to the extension.

Why a bridge at all? OpenClaw exposes Telegram/WhatsApp channels but no
local HTTP chat channel. The bridge gives the extension a CORS-friendly
HTTP surface scoped to chrome-extension:// origins and translates per-tab
session ids into OpenClaw --session-id args.

Same boot pattern as infer_bridge.py / sentinel-miniapp-v2/bridge.py:
launched via pythonw from START_AI_STACK.bat, audit log in
metamcp-local/logs/, port 8093 (8xxx live range, unallocated).
"""

import http.server
import hmac
import json
import logging
import os
import re
import shlex
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8101  # 8090-8100 are Hyper-V reserved on this host; first free 8xxx is 8101
WSL_DISTRO = "Ubuntu-24.04"
OPENCLAW_CLI = "/home/azfar/.npm-global/lib/node_modules/openclaw/dist/index.js"
DEFAULT_TIMEOUT_S = 600        # OpenClaw's own default
HARD_TIMEOUT_S = 900           # subprocess kill-fence on top
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# CREATE_NO_WINDOW (0x08000000) suppresses the console window Windows would
# otherwise allocate when pythonw spawns a console app like wsl.exe.
# Without this, every /chat request flashed a black terminal at the user.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOG_DIR = os.path.expandvars(r"%USERPROFILE%\metamcp-local\logs")
LOG_PATH = os.path.join(LOG_DIR, "openclaw_bridge.log")
AUDIT_PATH = os.path.join(LOG_DIR, "openclaw_bridge.jsonl")

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("openclaw-bridge")
_START_TS = time.time()


def _load_bridge_token() -> str:
    """Shared auth token for /chat — closes the unauthenticated S1 hole. From the
    COMET_BRIDGE_TOKEN env var, else metamcp-local/.env.local. Empty = unprovisioned
    → the gate fails OPEN (logs a warning) so deploying the code never breaks the
    panel before the token is set; enforcement turns on once a token exists."""
    t = os.environ.get("COMET_BRIDGE_TOKEN", "").strip()
    if t:
        return t
    envp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env.local")
    try:
        with open(envp, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip().startswith("COMET_BRIDGE_TOKEN"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


_BRIDGE_TOKEN = _load_bridge_token()
if _BRIDGE_TOKEN:
    logger.info("auth gate ENABLED on /chat (X-Comet-Token required)")
else:
    logger.warning("COMET_BRIDGE_TOKEN unset — /chat is UNAUTHENTICATED (S1 hole open). "
                   "Provision a token in .env.local to enable the gate.")


def _audit(record: dict) -> None:
    try:
        record["ts"] = time.time()
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # never let logging break the request
        logger.warning("audit write failed: %s", exc)


def _win_to_wsl(p: str) -> str:
    """C:\\Users\\x\\f.txt -> /mnt/c/Users/x/f.txt (drive-letter lowercased)."""
    return f"/mnt/{p[0].lower()}{p[2:].replace(os.sep, '/')}"


def run_openclaw_turn(session_id: str, message: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
    """Invoke one OpenClaw agent turn, return parsed result dict.

    Raises subprocess.TimeoutExpired or ValueError on parse failure.
    """
    # Pass the message OUT-OF-BAND via temp files — inlining it into
    # `wsl.exe -- bash -lc "<...>"` is unsafe because the Windows->wsl->bash
    # transport strips shlex's single-quoting, so backticks / $() / code fences
    # in the message get executed by bash ("unexpected EOF matching backtick" ->
    # the turn dies before the model runs). The message bytes go to a temp file;
    # a temp script (quoting preserved on disk) reads it into "$MSG" and execs
    # node; only `bash <plain-path>` crosses the wsl boundary. (Mirrors the fix in
    # openclaw/brain_wrapper.py:openclaw_one_shot — see test_wsl_quoting_transport.)
    logger.info("agent turn start session=%s msg_len=%d", session_id, len(message))
    msg_fd, msg_win = tempfile.mkstemp(suffix=".txt", prefix="oc_msg_")
    scr_fd, scr_win = tempfile.mkstemp(suffix=".sh", prefix="oc_run_")
    t0 = time.time()
    try:
        with os.fdopen(msg_fd, "w", encoding="utf-8", newline="") as f:
            f.write(message)
        script = (
            f"MSG=\"$(cat {shlex.quote(_win_to_wsl(msg_win))})\"\n"
            f"exec node {shlex.quote(OPENCLAW_CLI)} agent "
            f"--session-id {shlex.quote(session_id)} "
            f"--message \"$MSG\" "
            f"--json --timeout {int(timeout_s)}\n"
        )
        with os.fdopen(scr_fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        cmd = ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc",
               f"bash {_win_to_wsl(scr_win)}"]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=HARD_TIMEOUT_S,
            creationflags=_NO_WINDOW,
        )
    finally:
        for _p in (msg_win, scr_win):
            try:
                os.unlink(_p)
            except OSError:
                pass
    elapsed_ms = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        logger.error(
            "openclaw nonzero rc=%d stderr=%s", proc.returncode, proc.stderr[-2000:]
        )
        return {
            "_bridge_error": True,
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr[-2000:],
            "elapsed_ms": elapsed_ms,
        }
    raw = proc.stdout.strip()
    if not raw:
        return {
            "_bridge_error": True,
            "returncode": 0,
            "stderr_tail": "empty stdout from openclaw agent",
            "elapsed_ms": elapsed_ms,
        }
    # openclaw --json prints exactly one JSON document
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # fallback: pull the last {...} block
        last_brace = raw.rfind("{")
        try:
            data = json.loads(raw[last_brace:])
        except Exception:
            logger.error("JSON parse failed: %s. Raw head: %s", exc, raw[:500])
            return {
                "_bridge_error": True,
                "returncode": 0,
                "stderr_tail": f"JSON parse failed: {exc}",
                "elapsed_ms": elapsed_ms,
                "raw_head": raw[:500],
            }
    data["_elapsed_ms"] = elapsed_ms
    return data


def extract_reply(turn: dict) -> dict:
    """Pull the user-visible fields out of an openclaw agent --json payload."""
    if turn.get("_bridge_error"):
        return {
            "ok": False,
            "error": "bridge_error",
            "detail": turn.get("stderr_tail", "")[-1200:],
            "duration_ms": turn.get("elapsed_ms", 0),
        }
    payloads = (turn.get("result") or {}).get("payloads") or []
    text_parts = [p.get("text", "") for p in payloads if p.get("text")]
    media = [p.get("mediaUrl") for p in payloads if p.get("mediaUrl")]
    agent_meta = ((turn.get("result") or {}).get("meta") or {}).get("agentMeta", {}) or {}
    return {
        "ok": turn.get("status") == "ok",
        "reply": "\n\n".join(text_parts).strip(),
        "media": media,
        "session_id": agent_meta.get("sessionId"),
        "run_id": turn.get("runId"),
        "summary": turn.get("summary"),
        "duration_ms": turn.get("_elapsed_ms") or (
            (turn.get("result") or {}).get("meta") or {}
        ).get("durationMs"),
        "model": agent_meta.get("model"),
        "provider": agent_meta.get("provider"),
        "usage": agent_meta.get("usage"),
        # Session-window utilization fields. usage.input is the cumulative
        # prompt size for this turn — when it approaches contextTokens, the
        # next turn will start compacting. Surface both so the sidepanel
        # can render a "115,749 / 131,072" type counter.
        "context_limit": agent_meta.get("contextTokens"),
        "prompt_tokens": agent_meta.get("promptTokens"),
    }


# ── HTTP server ─────────────────────────────────────────────────────────────
class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "comet-sidepanel-bridge/0.1"

    # ── CORS ────────────────────────────────────────────────────────────
    def _write_cors(self) -> None:
        # Allow any chrome-extension:// origin — loopback bind already limits exposure.
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://") or origin.startswith("http://127.0.0.1"):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Comet-Token")
        self.send_header("Access-Control-Max-Age", "600")

    def _authed(self) -> bool:
        """True if the request carries the shared bridge token (or the gate is
        unprovisioned). Token via X-Comet-Token header, or ?token= for EventSource."""
        if not _BRIDGE_TOKEN:
            return True
        got = self.headers.get("X-Comet-Token", "")
        if not got:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got = (q.get("token") or [""])[0]
        return bool(got) and hmac.compare_digest(got, _BRIDGE_TOKEN)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._write_cors()
        self.end_headers()

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._write_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ── routes ──────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {
                "ok": True,
                "uptime_s": int(time.time() - _START_TS),
                "port": PORT,
            })
            return
        if parsed.path == "/events":
            # SSE placeholder for streaming progress; v2.
            self.send_response(501)
            self._write_cors()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"SSE not implemented yet")
            return
        self._send_json(404, {"ok": False, "error": "not_found", "path": parsed.path})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/chat":
            self._send_json(404, {"ok": False, "error": "not_found", "path": parsed.path})
            return
        if not self._authed():
            _audit({"event": "chat_denied", "reason": "bad_or_missing_token"})
            self._send_json(401, {"ok": False, "error": "unauthorized",
                                  "detail": "X-Comet-Token required"})
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 256 * 1024:
            self._send_json(400, {"ok": False, "error": "bad_length"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": "bad_json", "detail": str(exc)})
            return

        message = (body.get("message") or "").strip()
        session_id = (body.get("session_id") or "").strip() or "browser-default"
        if not message:
            self._send_json(400, {"ok": False, "error": "empty_message"})
            return
        if not SESSION_ID_RE.match(session_id):
            self._send_json(400, {"ok": False, "error": "bad_session_id", "got": session_id})
            return
        if len(message) > 32 * 1024:
            self._send_json(413, {"ok": False, "error": "message_too_long"})
            return

        _audit({"event": "chat_in", "session_id": session_id, "msg_len": len(message)})
        try:
            turn = run_openclaw_turn(session_id, message)
        except subprocess.TimeoutExpired:
            self._send_json(504, {"ok": False, "error": "timeout"})
            _audit({"event": "chat_timeout", "session_id": session_id})
            return
        except Exception as exc:
            logger.exception("chat handler crashed")
            self._send_json(500, {"ok": False, "error": "internal", "detail": str(exc)})
            _audit({"event": "chat_crash", "session_id": session_id, "exc": str(exc)})
            return

        reply = extract_reply(turn)
        status = 200 if reply.get("ok") else 502
        _audit({
            "event": "chat_out",
            "session_id": session_id,
            "ok": reply.get("ok"),
            "duration_ms": reply.get("duration_ms"),
            "model": reply.get("model"),
            "usage": reply.get("usage"),
            "reply_chars": len(reply.get("reply") or ""),
        })
        self._send_json(status, reply)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)


class _ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    logger.info("starting on %s:%d", HOST, PORT)
    print(f"Comet sidepanel bridge listening on http://{HOST}:{PORT}", flush=True)
    server = _ThreadedServer((HOST, PORT), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("interrupt — shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
