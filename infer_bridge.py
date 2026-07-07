"""
Inference Bridge — transparent gating proxy on port 8095.

Sits between OpenClaw and the local inference backend (llama-swap on :1234, which
fronts llama-server). OpenClaw's LM-Studio-compatible baseUrl points here:
    "baseUrl": "http://127.0.0.1:8095/v1"

Roles (all it actually does):
  GET  /infer_status          → {"active", "model", "blocked", "loaded"}  (crib polls)
  GET  /health,/healthz        → local liveness, DECOUPLED from model-resident
  POST /infer_block,/infer_unblock → the gaming/FLUX gate (set _blocked + kill in-flight)
  POST *completions*           → gate on _blocked (queue-wait via the GPU broker on a
                                 block), audit, then proxy to :1234
  everything else              → transparent proxy to :1234

Single model. The backend serves exactly `qwen/qwen3.6-27b` (llama-swap routes by the
`model` field, which OpenClaw already sets correctly), so the bridge does NOT classify
or rewrite the model — it's a pass-through. (Historical note: this file used to carry a
3-way classifier + multi-model router + fallback chain from the LM-Studio era; all three
classes were pinned to qwen3.6-27b on 2026-05-21, making it dead code, removed
2026-06-15 in the inference-stack consolidation. See
metamcp-local/openclaw/planning/inference-stack-consolidation-review.md.)
"""

import http.client
import http.server
import json
import logging
import os
import threading
import time
import urllib.request

try:
    import keyring
except ImportError:
    keyring = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Request audit log ─────────────────────────────────────────────────────────
# Bridge runs as pythonw (no console), so default logging goes nowhere.
# Persist every inference request to a JSONL file so we can audit who's calling,
# when, with what, and how big. One line per request. Append-only; rotation is
# external (single-user owner traffic is light).
import json as _audit_json
_AUDIT_PATH = os.path.expandvars(r"%USERPROFILE%\metamcp-local\logs\infer_bridge.jsonl")
try:
    os.makedirs(os.path.dirname(_AUDIT_PATH), exist_ok=True)
except Exception:
    pass


def _audit_log(record: dict) -> None:
    """Append one JSON line. Swallow any error — logging must never break the proxy."""
    try:
        record["ts"] = time.time()
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(_audit_json.dumps(record) + "\n")
    except Exception:
        pass


PORT    = 8095
LM_HOST = "127.0.0.1"
LM_PORT = 1234

# The one model the backend serves. Used only for /infer_status display when the
# live /v1/models list is unavailable — the bridge does not route or rewrite.
DEFAULT_MODEL = "qwen/qwen3.6-27b"

# Cold model load can take 60–120 s before the first response byte.
PROXY_TIMEOUT = 600

# axis-3 2.3 (ADR AI-009) — cold-path bound. When the backend isn't ready (cold-load
# or wedged), cap how long we wait for the FIRST response byte before failing fast with
# 503 Retry-After, instead of letting a turn hang the full PROXY_TIMEOUT on a wedged
# backend (the ~6-min-hang → 900 s-kill symptom). A clean cold-load is ~42 s, a contended
# one up to ~200 s, a wedge ≥ llama-swap's 360 s health timeout — so 240 s serves a legit
# load yet fails a wedge fast. http.client's timeout is PER-OPERATION, so this bounds the
# wait-for-first-byte but NOT a streaming generation (a warm turn keeps the 600 s budget).
WARM_TIMEOUT = float(os.environ.get("INFER_WARM_TIMEOUT_S", "240"))
_BACKEND_STATE_TTL = 2.5   # cache the readiness probe so a warm turn adds ~no latency

# GPU broker FIFO queue (watchdog :8200). When inference is blocked (a FLUX render
# or a game holds the 24 GB card), instead of an instant 503 we register in the
# broker's FIFO queue — which notifies the owner "🟡 your request is queued behind
# X" — and WAIT for the broker to auto-dispatch (unblock Qwen) when the card frees,
# then proxy through. We gate on our own `_blocked` flag (the source of truth, so we
# never serve during a real block); the queue drives the dispatch + the notify.
GPU_BROKER_URL = os.environ.get("GPU_BROKER_URL", "http://127.0.0.1:8200").rstrip("/")
QUEUE_MAX_WAIT_S = float(os.environ.get("INFER_QUEUE_MAX_WAIT_S", "170"))  # < OpenClaw's 180s ceiling
QUEUE_POLL_S = 3.0


def _broker_token() -> str:
    t = os.environ.get("GPU_BROKER_TOKEN", "").strip()
    if t:
        return t
    if keyring:
        try:
            return keyring.get_password("sentinel-watchdog", "gpu-broker-client") or ""
        except Exception:
            return ""
    return ""


def _broker(path: str, body: dict) -> dict | None:
    """POST to the GPU broker; None on any failure (caller falls back gracefully)."""
    try:
        headers = {"Content-Type": "application/json"}
        tok = _broker_token()
        if tok:
            headers["X-Sentinel-Service-Token"] = tok
        req = urllib.request.Request(GPU_BROKER_URL + path,
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.warning("gpu-broker %s failed: %s", path, e)
        return None


# ── Concurrency model — INTENTIONAL for personal-scale traffic ───────────────
# This bridge serves 1–3 concurrent inference requests in normal owner usage.
# The shared-globals + single-lock pattern is fine at that scale but won't age
# into multi-tenant / high-QPS without rework (bounded concurrency, per-tenant
# queues, 429 backpressure, metrics). Deliberate ceiling until V5.
# ─────────────────────────────────────────────────────────────────────────────
_active_count       = 0
_current_model      = DEFAULT_MODEL
_blocked            = False
_active_connections: list = []
_lock               = threading.Lock()

# Loaded-models cache for /infer_status (brief TTL).
_LOADED_TTL          = 15.0
_loaded_models: list = []
_loaded_expires: float = 0.0

# axis-3 2.3 — real backend readiness (the :5800 model behind llama-swap), surfaced
# SEPARATELY from the bridge's own process liveness so /health stays decoupled (no
# false-DOWN restart storm). `_warm_lock` is single-flight: one cold-load warmer at a
# time, so concurrent cold requests don't pile sockets onto a loading backend.
_backend_state_cache   = ""        # "ready" | "loading" | "down"
_backend_state_expires = 0.0
_warm_lock             = threading.Lock()

_lm_api_key_warned = False


def _lm_api_key() -> str:
    """LM Studio API key from WCM (same entry the watchdog uses). One-time warning
    if missing — without it /v1/models returns 401 and /infer_status can't show
    what's loaded."""
    global _lm_api_key_warned
    if not keyring:
        if not _lm_api_key_warned:
            logger.warning("keyring module unavailable — backend requests unauthenticated")
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
        logger.warning("WCM has no sentinel-watchdog/lm_api_key — /v1/models may 401")
        _lm_api_key_warned = True
    return key


_STRIP_REQ  = {"connection", "keep-alive", "te", "trailers",
               "transfer-encoding", "upgrade", "content-length"}
_STRIP_RESP = {"connection", "keep-alive", "te", "trailers",
               "transfer-encoding", "upgrade"}


def _get_loaded_models(force: bool = False) -> list:
    """Model ids currently loaded on the backend (for /infer_status). Cached briefly."""
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
            logger.warning("/v1/models returned %d", resp.status)
            return _loaded_models  # keep stale rather than wipe
        data = json.loads(body)
        ids = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        _loaded_models = ids
        _loaded_expires = now + _LOADED_TTL
        return ids
    except Exception as e:
        logger.warning("Could not fetch /v1/models: %s", e)
        return _loaded_models


def _backend_state(force: bool = False) -> str:
    """Real backend readiness via llama-swap /running (NOT /v1/models, which lists the
    configured model even when it's unloaded). Returns 'ready' (resident + serving),
    'loading' (cold-loading), or 'down' (unloaded / unreachable). Side-effect-free
    (a /running poll does NOT trigger a load) and cached briefly so a warm turn adds
    ~no latency. axis-3 2.3 / ADR AI-009."""
    global _backend_state_cache, _backend_state_expires
    now = time.time()
    if not force and now < _backend_state_expires and _backend_state_cache:
        return _backend_state_cache
    state = "down"
    try:
        conn = http.client.HTTPConnection(LM_HOST, LM_PORT, timeout=3)
        conn.request("GET", "/running")
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        if resp.status == 200:
            running = (json.loads(raw) or {}).get("running", []) or []
            states = [m.get("state") for m in running]
            if "ready" in states:
                state = "ready"
            elif any(s in ("starting", "loading") for s in states):
                state = "loading"
            else:
                state = "down"
    except Exception:
        state = "down"
    _backend_state_cache = state
    _backend_state_expires = now + _BACKEND_STATE_TTL
    return state


# ── Handler ───────────────────────────────────────────────────────────────────

class BridgeHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.0 closes the connection after each response — signals end-of-body
    # without Content-Length or chunked encoding.
    protocol_version = "HTTP/1.0"

    def _status(self):
        with _lock:
            active  = _active_count > 0
            model   = _current_model
            blocked = _blocked
        # Prefer what the backend actually reports loaded, so the UI stays honest.
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

    def _send_json(self, status: int, obj: dict, retry_after=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def _health(self):
        """Local liveness — the bridge PROCESS being up, DECOUPLED from whether the
        upstream Qwen model is loaded. (GET /health used to fall through to _proxy →
        forwarded to :1234, so the watchdog HTTP probe went false-DOWN every time the
        broker evicted Qwen → a restart storm, ≈43/day on 2026-06-12.) The bridge is a
        proxy; its health is "am I up and serving", not "is the model warm". Qwen
        residency is observed separately (llama-swap /running, broker).

        `ok` stays decoupled (process liveness); `backend` (axis-3 2.3) surfaces the
        REAL model readiness for observers (supervisor/UI) WITHOUT re-coupling `ok`."""
        with _lock:
            blocked = _blocked
            active = _active_count > 0
        self._send_json(200, {"ok": True, "service": "infer-bridge",
                              "blocked": blocked, "active": active,
                              "backend": _backend_state()})

    def _wait_for_unblock(self) -> bool:
        """Inference is blocked → register in the broker FIFO queue (which notifies
        the owner "🟡 queued behind X" + orders waiters), then wait for the broker to
        auto-dispatch — i.e. unblock Qwen — when the card frees, polling to drive the
        dispatch. Gates on our OWN `_blocked` flag (the truth) so a genuine block never
        serves. Returns True to proceed, False to 503. Broker unreachable → False
        (fall back to the old instant-503 behavior)."""
        holder = (self.headers.get("User-Agent") or "openclaw")[:40]
        r = _broker("/api/v2/gpu-broker/queue/enter", {"consumer": "qwen", "holder": holder})
        if r is None:
            return False                      # broker down → don't hold; instant 503
        if r.get("ready"):
            return True
        ticket = r.get("ticket")
        if not ticket:
            return True
        logger.info("Inference queued behind %s (#%s) — waiting up to %.0fs",
                    r.get("behind", "?"), r.get("position", "?"), QUEUE_MAX_WAIT_S)
        deadline = time.time() + QUEUE_MAX_WAIT_S
        while time.time() < deadline:
            with _lock:
                cleared = not _blocked
            if cleared:
                _broker("/api/v2/gpu-broker/queue/leave", {"ticket": ticket})
                logger.info("Inference unblocked — proceeding (auto-resumed from queue)")
                return True
            _broker("/api/v2/gpu-broker/queue/poll", {"ticket": ticket})   # drive dispatch
            time.sleep(QUEUE_POLL_S)
        _broker("/api/v2/gpu-broker/queue/leave", {"ticket": ticket})
        return False

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
        logger.info("Inference bridge %s", "blocked" if block else "unblocked")
        body = json.dumps({"ok": True, "blocked": block}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method: str):
        global _active_count, _current_model, _backend_state_expires
        is_inference = method == "POST" and "completions" in self.path
        is_cold = False
        got_warm = False

        if is_inference:
            with _lock:
                blocked_now = _blocked
            # Blocked → try the FIFO queue + auto-resume instead of an instant 503.
            # _wait_for_unblock holds NO lock while waiting, so other requests flow.
            if blocked_now and not self._wait_for_unblock():
                self._send_json(503, {"error": "GPU busy — your request was queued but the wait "
                                      "exceeded the budget; retry shortly", "retry_after_s": 15},
                                retry_after="15")
                logger.info("Inference 503 — queue wait exceeded / broker unreachable")
                return
            # axis-3 2.3 — backend-aware cold path. If the model isn't ready, SINGLE-FLIGHT
            # one warmer (concurrent cold requests get a fast 503, no socket pile-up onto a
            # loading backend) and bound the warmer's wait-for-first-byte to WARM_TIMEOUT, so
            # a WEDGE fails fast (503 Retry-After) instead of hanging the full PROXY_TIMEOUT.
            is_cold = _backend_state() != "ready"
            if is_cold:
                got_warm = _warm_lock.acquire(blocking=False)
                if not got_warm:
                    self._send_json(503, {"error": "backend warming up — retry shortly",
                                          "retry_after_s": 5}, retry_after="5")
                    logger.info("Inference 503 — backend warming (single-flight)")
                    return
            with _lock:
                _active_count += 1
            logger.info("Inference started  (active=%d cold=%s)", _active_count, is_cold)

        conn = None
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            # Single model → pass the body through unchanged (no classify / rewrite).
            # Record a slim audit line for who/what/when.
            if is_inference and body:
                try:
                    import hashlib as _h
                    data     = json.loads(body)
                    messages = data.get("messages", [])
                    model    = data.get("model", "") or DEFAULT_MODEL
                    with _lock:
                        _current_model = model
                    last_user = ""
                    for m in reversed(messages):
                        if isinstance(m, dict) and m.get("role") == "user":
                            c = m.get("content", "")
                            if isinstance(c, list):
                                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                            last_user = str(c)
                            break
                    _audit_log({
                        "client":        self.client_address[0],
                        "path":          self.path,
                        "user_agent":    self.headers.get("User-Agent", ""),
                        "model":         model,
                        "body_bytes":    len(body) if body else 0,
                        "msg_count":     len(messages),
                        "prompt_first80": last_user[:80],
                        "prompt_sha256": _h.sha256(last_user.encode()).hexdigest()[:16],
                    })
                except Exception as e:
                    logger.warning("Audit parse error: %s", e)
                    _audit_log({"client": self.client_address[0], "path": self.path,
                                "user_agent": self.headers.get("User-Agent", ""),
                                "audit_error": str(e)})

            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() not in _STRIP_REQ}

            # Cold path bounds the wait-for-first-byte (a wedge fails fast); a warm/ready
            # turn keeps the full PROXY_TIMEOUT so a long generation isn't cut (axis-3 2.3).
            conn = http.client.HTTPConnection(
                LM_HOST, LM_PORT, timeout=(WARM_TIMEOUT if is_cold else PROXY_TIMEOUT))
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
                if is_cold:
                    # The backend didn't produce a first byte within WARM_TIMEOUT → it's
                    # wedged (or a very slow load). Fail FAST with 503 so the caller retries
                    # in seconds (the auto-recovery supervisor heals the backend) instead of
                    # hanging the full 600 s → 900 s-kill. (No-op if a response already began.)
                    self._send_json(503, {"error": "backend not ready (cold-load exceeded "
                                          f"{int(WARM_TIMEOUT)}s — likely wedged); retry shortly",
                                          "retry_after_s": 10}, retry_after="10")
                    logger.info("Inference 503 — cold backend not ready within %.0fs", WARM_TIMEOUT)
                else:
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
            if got_warm:
                # Done warming — let the next cold request take over, and invalidate the
                # readiness cache so it re-probes the (now possibly ready) backend.
                _backend_state_expires = 0.0
                _warm_lock.release()
            if is_inference:
                with _lock:
                    _active_count -= 1
                logger.info("Inference finished (active=%d)", _active_count)

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/infer_status":
            self._status()
        elif p in ("/health", "/healthz"):
            self._health()        # local liveness — never proxied (see _health)
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
    logger.info("Inference bridge on :%d → backend %s:%d (single model %s)",
                PORT, LM_HOST, LM_PORT, DEFAULT_MODEL)
    logger.info("Status : http://127.0.0.1:%d/infer_status", PORT)
    server.serve_forever()
