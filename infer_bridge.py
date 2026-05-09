"""
Inference Bridge — transparent HTTP proxy on port 8095.

Roles:
  GET  /infer_status   → {"active": bool, "model": str}  (polled by power-monitor)
  POST *completions*   → classify prompt complexity, rewrite model field, proxy to LM Studio
  everything else      → transparent proxy to LM Studio at 127.0.0.1:1234

Model routing (3-way):
  Simple prompts  → qwen/qwen3.5-9b                  (fast, light)
  Complex prompts → qwen/qwen3.6-27b                 (default, capable)
  Coding prompts  → qwen/qwen2.5-coder-32b-instruct  (code blocks / dev keywords)

Coding takes precedence: anything with a ``` fence or coding keyword routes to
the coder. Otherwise tool/complex/simple classification applies.

LM Studio swaps models on demand. With ~24 GB VRAM, only one of {27B chat,
32B coder} is loaded at a time — bridge rewrites the model field, LM Studio
loads the right one (~5-10s warm-disk swap).
If the target is not loaded, LM Studio returns an error and OpenClaw falls back.

OpenClaw LM Studio baseUrl must point here:
  "baseUrl": "http://127.0.0.1:8095/v1"
"""

import http.client
import http.server
import json
import logging
import threading
import time

try:
    import keyring
except ImportError:
    keyring = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PORT    = 8095
LM_HOST = "127.0.0.1"
LM_PORT = 1234

SIMPLE_MODEL  = "qwen/qwen3.5-9b"
COMPLEX_MODEL = "qwen/qwen3.6-27b"
CODING_MODEL  = "qwen/qwen2.5-coder-32b-instruct"  # install via LM Studio first

# Cold model load can take 60–120 s before first response byte
PROXY_TIMEOUT = 600

# ── Concurrency model — INTENTIONAL for personal-scale traffic ───────────────
# This bridge serves 1–3 concurrent inference requests in normal owner usage.
# The shared-globals + single-lock pattern below is fine at that scale but
# will not age into multi-tenant or high-QPS deployments without rework.
#
# Known scale ceiling:
#   - _active_count / _active_connections rely on shared mutable state
#   - Error paths in the proxy can leave status reporting briefly stale
#   - No bounded concurrency / queue / 429 backpressure
#
# When V5 (multi-tenant) lands, replace the globals with a state-manager
# class, add bounded concurrency + per-tenant queues, and emit metrics for
# active/rejected/aborted/fallback counts. Tracked in V5 TODO under
# "Phase A — Foundation". Until then, this is a deliberate ceiling.
# ─────────────────────────────────────────────────────────────────────────────
_active_count       = 0
_current_model      = COMPLEX_MODEL
_blocked            = False
_active_connections: list = []
_lock               = threading.Lock()

# Loaded-models cache so we can fall back when the intended target isn't loaded.
_LOADED_TTL          = 15.0  # seconds
_loaded_models: list = []
_loaded_expires: float = 0.0


_lm_api_key_warned = False


def _lm_api_key() -> str:
    """Pull LM Studio API key from WCM (same entry the watchdog uses).
    Emits a one-time warning if the key is missing — without it, /v1/models
    will return 401 from LM Studio and the model-availability resolver will
    never see what's loaded."""
    global _lm_api_key_warned
    if not keyring:
        if not _lm_api_key_warned:
            logger.warning("keyring module unavailable — LM Studio requests will be unauthenticated")
            _lm_api_key_warned = True
        return ""
    try:
        key = keyring.get_password("sentinel-watchdog", "lm_api_key") or ""
    except Exception as e:
        if not _lm_api_key_warned:
            logger.warning("Failed to read lm_api_key from WCM: %s", e)
            _lm_api_key_warned = True
        return ""
    if not key and not _lm_api_key_warned:
        logger.warning("WCM has no entry sentinel-watchdog/lm_api_key — LM Studio /v1/models will return 401")
        _lm_api_key_warned = True
    return key

_STRIP_REQ  = {"connection", "keep-alive", "te", "trailers",
               "transfer-encoding", "upgrade", "content-length"}
_STRIP_RESP = {"connection", "keep-alive", "te", "trailers",
               "transfer-encoding", "upgrade"}

# ── Complexity classifier ─────────────────────────────────────────────────────

_COMPLEX_KEYWORDS = {
    "analyze", "analyse", "explain", "implement", "write", "create", "debug",
    "compare", "summarize", "plan", "design", "calculate", "research",
    "refactor", "review", "generate", "build", "develop", "architecture",
    "translate", "convert", "optimize", "evaluate",
}

# Tool-requiring intents — 9B handles these poorly (hallucinates tool calls,
# emits malformed structured output). Force COMPLEX so 27B handles tool use.
_TOOL_KEYWORDS = {
    "calendar", "schedule", "remind", "reminder", "alarm", "todoist", "todo",
    "weather", "email", "gmail", "drive", "onedrive", "notes", "event",
    "appointment", "meeting", "deployment",
}

# Coding intents — route to CODING_MODEL (Qwen2.5-Coder-32B). Keep this list
# tight: false positives swap a heavy model unnecessarily.
_CODING_KEYWORDS = {
    "function", "class ", "refactor", "debug", "stacktrace", "stack trace",
    "implement", "fix this", "fix the bug", "review this code", "code review",
    "regex", "compile error", "linter", "typescript", "javascript",
    "python error", "rust", "golang ", "kotlin ", "swift ",
    "syntax error", "runtime error", "unit test", "test case",
    "git commit", "merge conflict", "pull request", "code snippet",
}

# Truly conversational queries — quick replies, no tool use needed.
_SIMPLE_KEYWORDS = {
    "how are you", "hello", "hi ", "hey", "thanks", "thank you",
    "good morning", "good afternoon", "good evening", "good night",
    "what time", "what day", "what date",
}


def _looks_like_tool_result(text: str) -> bool:
    """True if a user-role message is actually a tool result blob, not a real query."""
    stripped = text.strip()
    # JSON blobs or very long structured payloads aren't user queries
    return len(stripped) > 300 or stripped.startswith(("{", "["))


def _classify(messages: list) -> str:
    """Return SIMPLE_MODEL or COMPLEX_MODEL based on message content."""
    if not messages:
        return COMPLEX_MODEL

    user_texts = []   # all user-role messages in order
    convo_text = ""   # user prose + assistant prose — excludes system, tool calls, tool results
    for m in messages:
        role    = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        else:
            text = str(content)
        # Skip tool roles — their data volume doesn't reflect reasoning complexity
        if role in ("tool", "tool_result", "tool_results"):
            continue
        # For assistant messages, skip pure tool-call turns (no prose content)
        if role == "assistant" and not text.strip():
            continue
        if role in ("user", "assistant"):
            convo_text += text + " "
        if role == "user":
            user_texts.append(text)

    # No user messages at all (e.g. init/greeting request with only a system message)
    if not user_texts:
        return COMPLEX_MODEL

    # Use the last user message; if it looks like a tool-result blob injected into a user
    # role (common in multi-turn tool-call chains), walk back to find the real query.
    last_user_text = ""
    for t in reversed(user_texts):
        if not _looks_like_tool_result(t):
            last_user_text = t
            break
    if not last_user_text and user_texts:
        last_user_text = user_texts[-1]  # nothing clean found, use whatever we have

    # Normalize curly apostrophes/quotes so substring matches work
    last_lower = (last_user_text.lower()
                  .replace("’", "'")
                  .replace("‘", "'"))
    logger.info("Classifier: last_user=%r convo_len=%d",
                last_user_text[:120], len(convo_text))

    # Force coding: code blocks in conversation OR explicit coding keywords
    # (must come before complex/tool checks — coding wins)
    if "```" in convo_text:
        return CODING_MODEL
    if any(kw in last_lower for kw in _CODING_KEYWORDS):
        return CODING_MODEL

    # Force complex: explicit complex-intent keywords
    if any(kw in last_lower for kw in _COMPLEX_KEYWORDS):
        return COMPLEX_MODEL

    # Force complex: tool-requiring intents (9B handles tool calls poorly)
    if any(kw in last_lower for kw in _TOOL_KEYWORDS):
        return COMPLEX_MODEL

    # Simple keywords win before the length check — tool results inflate context
    # but don't make the user's question more complex
    if any(kw in last_lower for kw in _SIMPLE_KEYWORDS):
        return SIMPLE_MODEL

    # Long prose conversation without clear signal → complex
    if len(convo_text) > 3000:
        return COMPLEX_MODEL

    # Short unmatched prompt → simple
    if len(last_user_text) < 120:
        return SIMPLE_MODEL

    return COMPLEX_MODEL


# ── Model availability resolver ───────────────────────────────────────────────

def _get_loaded_models(force: bool = False) -> list:
    """Return list of model ids currently loaded in LM Studio. Cached briefly.
    Pass force=True to bypass the cache (e.g. user-initiated refresh)."""
    global _loaded_models, _loaded_expires
    now = time.time()
    if not force and now < _loaded_expires and _loaded_models:
        return _loaded_models
    try:
        conn = http.client.HTTPConnection(LM_HOST, LM_PORT, timeout=3)
        headers = {}
        key = _lm_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        conn.request("GET", "/v1/models", headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status != 200:
            logger.warning("LM /v1/models returned %d", resp.status)
            return _loaded_models  # keep stale list rather than wiping
        data = json.loads(body)
        ids = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        _loaded_models = ids
        _loaded_expires = now + _LOADED_TTL
        return ids
    except Exception as e:
        logger.warning("Could not fetch /v1/models: %s", e)
        return _loaded_models


def _resolve_target(intended: str) -> str:
    """Return a model that's actually loaded. Fall back gracefully when the
    intended target isn't there."""
    loaded = _get_loaded_models()
    if not loaded:
        return intended  # LM Studio unreachable — pass through, let it 404 if bad
    if intended in loaded:
        return intended

    # Fallback preference: try to keep the spirit of the request.
    fallback_chain = {
        CODING_MODEL:  [COMPLEX_MODEL, SIMPLE_MODEL],
        COMPLEX_MODEL: [CODING_MODEL,  SIMPLE_MODEL],
        SIMPLE_MODEL:  [COMPLEX_MODEL, CODING_MODEL],
    }.get(intended, [COMPLEX_MODEL, CODING_MODEL, SIMPLE_MODEL])

    for fb in fallback_chain:
        if fb in loaded:
            logger.info("Router fallback: %s not loaded → %s", intended, fb)
            return fb
    # Nothing matched — use whatever's loaded
    return loaded[0]


# ── Handler ───────────────────────────────────────────────────────────────────

class BridgeHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.0 closes connection after each response — signals end-of-body
    # to the client without needing Content-Length or chunked encoding.
    protocol_version = "HTTP/1.0"

    def _status(self):
        with _lock:
            active  = _active_count > 0
            model   = _current_model
            blocked = _blocked
        # If the cached _current_model isn't actually loaded right now, prefer
        # whatever LM Studio reports — keeps the UI honest.
        # Honour ?force=1 from the user-initiated refresh button.
        force = "force=1" in (self.path or "").lower()
        loaded = _get_loaded_models(force=force)
        if loaded and model not in loaded:
            model = loaded[0]
        body = json.dumps({
            "active":  active,
            "model":   model,
            "blocked": blocked,
            "loaded":  loaded,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _set_block(self, block: bool):
        global _blocked
        with _lock:
            _blocked = block
            conns_to_kill = list(_active_connections) if block else []
        for conn in conns_to_kill:
            try:
                conn.close()
            except Exception:
                pass
        if conns_to_kill:
            logger.info("Aborted %d in-flight inference connection(s)", len(conns_to_kill))
        state = "blocked" if block else "unblocked"
        logger.info("Inference bridge %s", state)
        body = json.dumps({"ok": True, "blocked": block}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method: str):
        global _active_count, _current_model
        is_inference = method == "POST" and "completions" in self.path

        if is_inference:
            with _lock:
                if _blocked:
                    body = json.dumps({"error": "inference blocked — gaming session active"}).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    logger.info("Inference request rejected — bridge is blocked (gaming)")
                    return
                _active_count += 1
            logger.info("Inference started  (active=%d)", _active_count)

        conn = None
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            # Classify prompt and rewrite the model field. Resolve to a model
            # that's actually loaded in LM Studio so we don't 404 on phantom
            # targets the user hasn't installed yet.
            if is_inference and body:
                try:
                    data     = json.loads(body)
                    messages = data.get("messages", [])
                    intended = _classify(messages)
                    target   = _resolve_target(intended)
                    original = data.get("model", "")
                    if target != original:
                        data["model"] = target
                        body = json.dumps(data).encode()
                    if target != intended:
                        logger.info("Router: %s → %s (intended %s, fallback)",
                                    original or "(none)", target, intended)
                    else:
                        logger.info("Router: %s → %s", original or "(none)", target)
                    with _lock:
                        _current_model = target
                except Exception as e:
                    logger.warning("Router error: %s", e)

            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() not in _STRIP_REQ}

            conn = http.client.HTTPConnection(LM_HOST, LM_PORT, timeout=PROXY_TIMEOUT)
            if is_inference:
                with _lock:
                    _active_connections.append(conn)
            conn.request(method, self.path, body=body, headers=fwd)
            resp = conn.getresponse()

            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in _STRIP_RESP:
                    self.send_header(k, v)
            self.end_headers()

            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

        except Exception as e:
            logger.error("Proxy error (%s %s): %s", method, self.path, e)
            try:
                self.send_error(502, f"Bridge proxy error: {e}")
            except Exception:
                pass
        finally:
            if conn:
                with _lock:
                    try:
                        _active_connections.remove(conn)
                    except ValueError:
                        pass
                try:
                    conn.close()
                except Exception:
                    pass
            if is_inference:
                with _lock:
                    _active_count -= 1
                logger.info("Inference finished (active=%d)", _active_count)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/infer_status":
            self._status()
        else:
            self._proxy("GET")

    def do_POST(self):
        if self.path == "/infer_block":
            self._set_block(True)
        elif self.path == "/infer_unblock":
            self._set_block(False)
        else:
            self._proxy("POST")
    def do_PUT(self):     self._proxy("PUT")
    def do_DELETE(self):  self._proxy("DELETE")
    def do_OPTIONS(self): self._proxy("OPTIONS")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), BridgeHandler)
    logger.info("Inference bridge on :%d → LM Studio %s:%d", PORT, LM_HOST, LM_PORT)
    logger.info("Simple  model : %s", SIMPLE_MODEL)
    logger.info("Complex model : %s", COMPLEX_MODEL)
    logger.info("Status  : http://127.0.0.1:%d/infer_status", PORT)
    server.serve_forever()
