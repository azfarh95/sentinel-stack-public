"""P4.4 + P6 — authed, hardened HTTP surface for the browser assistant.

Lets a task be TRIGGERED beyond the local shell (a panel button, a Telegram
command, curl) — gated, owner-in-the-loop, brain-logged, observable.

  GET  /health               → {ok, busy, stuck, current, uptime_s}
  GET  /metrics              → task-level telemetry (success rate, status mix,
                               approval rate, fence trips, vision/ground mix, durations)
  POST /run {task, mode?, steps?, wall?, vision?, channel?}
        mode    : "headless" (default, isolated) | "comet" (attach real Comet via CDP)
        channel : "telegram" (default — approve from your phone) | "console" | "none"
        → runs a GATED task (P4.1 action gate + P4.3 domain guard + the chosen
          approval channel) and returns the result record.

Auth: X-Comet-Token header (same shared token as the comet bridge; from
COMET_BRIDGE_TOKEN env / .env.local). Loopback-bound. One task at a time — a
concurrent /run gets 409 (browser + the single GPU slot serialize anyway).

P6 hardening (so it's safe to leave on, monitored by the watchdog):
  - rate guard      : at most MAX_RUNS_PER_HOUR accepted /run per rolling hour (429).
  - wall clamp      : a requested wall is clamped to [MIN_WALL, MAX_WALL] — a caller
                      can't ask for an unbounded session that monopolises the GPU.
  - kill-switch     : `mode.py off` → 503 (the rollback path; chat unaffected).
  - stuck reaper    : a background thread watches the in-flight task. If the in-agent
                      fence somehow fails to terminate, it alerts (Telegram) at a soft
                      grace and force-kills the process tree (chrome children included
                      → no leaked sessions) at a hard ceiling; the scheduled task
                      restarts the surface.

Run:  .venv\\Scripts\\python server.py
Call: curl -s -XPOST 127.0.0.1:8108/run -H "X-Comet-Token: <tok>" \\
           -H "Content-Type: application/json" -d '{"task":"..."}'
"""
import asyncio
import hmac
import http.server
import json
import os
import socketserver
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

HOST, PORT = "127.0.0.1", 8108

# --- P6 guards / reaper tunables -------------------------------------------
MIN_WALL, MAX_WALL = 30.0, 600.0      # a sync /run's wall fence is clamped into this band
PANEL_MAX_WALL = 1800.0                # panel runs allow longer walls — approval waits are
                                       # idle (no GPU); inference is bounded by max_steps
PANEL_APPROVE_TIMEOUT = 240.0          # per-action: deny if the panel doesn't answer in time
MAX_RUNS_PER_HOUR = 30                 # rolling-window rate guard

# --- P3 convergence: serve the panel as a portable web app + proxy shopping --
SHOP_API = "http://127.0.0.1:8100/api/search"
APP_DIR = Path(__file__).resolve().parent / "extension"   # one source, two deployments
# Allowlist of files served at /app/ — NEVER config.local.js (it holds the token).
_APP_ALLOW = {"sidepanel.html", "sidepanel.js", "sidepanel.css",
              "icons/icon16.png", "icons/icon48.png", "icons/icon128.png"}
_CT = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
       ".css": "text/css; charset=utf-8", ".png": "image/png"}
REAP_POLL_S = 15.0                     # how often the reaper checks the in-flight task
SOFT_GRACE_S = 90.0                    # elapsed > wall + this  → alert (fence should have fired)
HARD_GRACE_S = 300.0                   # elapsed > wall + this  → kill the process tree


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
_lock = threading.Lock()
_START = time.time()

# --- in-flight task state (read by /health + the reaper) -------------------
_state_lock = threading.Lock()
_current: dict | None = None           # {task, started, wall, label} while a run is in flight
_stuck = False                         # set by the reaper once it has alerted on the current task
_starts: deque = deque()               # accepted-run start timestamps for the rate guard


def _notify(text: str) -> None:
    """Best-effort owner ping (reuses the testbot). Never raises."""
    try:
        from approval_telegram import load_testbot_creds
        token, chat = load_testbot_creds()
        if not token:
            return
        data = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass


def _reaper() -> None:
    """Backstop for the in-agent wall fence: alert then force-kill a stuck run."""
    global _stuck
    while True:
        time.sleep(REAP_POLL_S)
        with _state_lock:
            cur = dict(_current) if _current else None
            stuck = _stuck
        if not cur:
            if stuck:
                with _state_lock:
                    _stuck = False
            continue
        elapsed = time.time() - cur["started"]
        wall = cur.get("wall", MAX_WALL)
        if elapsed > wall + HARD_GRACE_S:
            _notify(f"🌐⛔ Browser surface STUCK {int(elapsed)}s (fence failed) — force-restarting.\n"
                    f"task: {str(cur.get('task'))[:120]}")
            try:
                # /T kills the whole tree (the headless chrome children too → no leak).
                subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                os._exit(1)
            return
        if elapsed > wall + SOFT_GRACE_S and not stuck:
            with _state_lock:
                _stuck = True
            _notify(f"🌐⚠️ Browser surface task running {int(elapsed)}s (> wall {int(wall)}s+grace). "
                    f"Watching; will force-restart at {int(wall + HARD_GRACE_S)}s.\n"
                    f"task: {str(cur.get('task'))[:120]}")


def _rate_ok() -> bool:
    """True if accepting a run now stays within MAX_RUNS_PER_HOUR (rolling)."""
    now = time.time()
    with _state_lock:
        while _starts and now - _starts[0] > 3600:
            _starts.popleft()
        if len(_starts) >= MAX_RUNS_PER_HOUR:
            return False
        _starts.append(now)
        return True


def _make_approver(channel: str):
    if channel == "none":
        return None
    if channel == "console":
        from tools_gated import console_approve
        return console_approve
    # default: telegram
    from approval_telegram import load_testbot_creds, make_telegram_approver
    tok, chat = load_testbot_creds()
    return make_telegram_approver(tok, chat, timeout_s=180) if tok else None


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "browser-assistant-surface/0.2"

    def _json(self, code, body):
        b = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self) -> bool:
        if not _TOKEN:
            return True  # unprovisioned → fail open (warned at startup)
        got = self.headers.get("X-Comet-Token", "")
        return bool(got) and hmac.compare_digest(got, _TOKEN)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            with _state_lock:
                cur = dict(_current) if _current else None
                stuck = _stuck
            current = None
            if cur:
                current = {"task": str(cur.get("task"))[:140], "caller": cur.get("caller"),
                           "elapsed_s": int(time.time() - cur["started"]),
                           "wall_s": cur.get("wall")}
            from mode import browser_enabled
            self._json(200, {"ok": True, "enabled": browser_enabled(),
                             "busy": _lock.locked(), "stuck": stuck,
                             "current": current, "uptime_s": int(time.time() - _START)})
            return
        if path == "/metrics":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                from metrics import compute
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                hours = float(q["hours"][0]) if q.get("hours") else None
                self._json(200, {"ok": True, "all": compute(None), "windowed": compute(hours) if hours else None})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": "metrics", "detail": str(e)[:200]})
            return
        if path == "/events":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = (q.get("job", [""])[0]).strip()
            cursor = int(q.get("cursor", ["0"])[0] or 0)
            from panel_jobs import get_job
            job = get_job(jid)
            if not job:
                self._json(404, {"ok": False, "error": "no_such_job"})
                return
            self._json(200, job.since(cursor))
            return
        # --- portable web app (P3): one source served to any surface ---------
        if path == "/app":
            self.send_response(302)
            self.send_header("Location", "/app/")
            self.end_headers()
            return
        if path.startswith("/app/"):
            self._serve_app(path[len("/app/"):])
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def _serve_app(self, rel: str):
        rel = rel or "sidepanel.html"
        if rel in ("", "index.html"):
            rel = "sidepanel.html"
        if rel not in _APP_ALLOW:                 # blocks config.local.js + path traversal
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            data = (APP_DIR / rel).read_bytes()
        except Exception:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        ct = _CT.get("." + rel.rsplit(".", 1)[-1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0 or n > 64 * 1024:
            self._json(400, {"ok": False, "error": "bad_length"})
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            self._json(400, {"ok": False, "error": "bad_json", "detail": str(e)})
            return None

    def do_POST(self):
        global _current, _stuck
        path = urllib.parse.urlparse(self.path).path
        if not self._authed():
            self._json(401, {"ok": False, "error": "unauthorized", "detail": "X-Comet-Token required"})
            return

        # --- inline approval from the side panel (no lock needed) -----------
        if path == "/approve":
            body = self._read_body()
            if body is None:
                return
            from panel_jobs import get_job
            job = get_job((body.get("job") or "").strip())
            if not job:
                self._json(404, {"ok": False, "error": "no_such_job"})
                return
            ok = job.resolve((body.get("id") or "").strip(), bool(body.get("decision")))
            self._json(200, {"ok": ok})
            return

        # --- shop (P3): fast product search, no LLM — proxy to the shopping MCP ---
        if path == "/shop":
            body = self._read_body()
            if body is None:
                return
            q = (body.get("query") or "").strip()
            if not q:
                self._json(400, {"ok": False, "error": "query required"})
                return
            payload = json.dumps({"query": q, "marketplaces": body.get("marketplaces", "all"),
                                  "top_n": int(body.get("top_n", 10)),
                                  "max_price_sgd": body.get("max_price_sgd")}).encode()
            try:
                req = urllib.request.Request(SHOP_API, data=payload,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=130) as r:
                    self._json(200, json.loads(r.read().decode()))
            except Exception as e:  # noqa: BLE001
                self._json(502, {"ok": False, "error": f"shopping backend: {type(e).__name__}: {str(e)[:160]}"})
            return

        if path != "/run":
            self._json(404, {"ok": False, "error": "not_found"})
            return

        from mode import browser_enabled
        if not browser_enabled():
            self._json(503, {"ok": False, "error": "browser mode disabled (kill-switch)"})
            return
        body = self._read_body()
        if body is None:
            return
        task = (body.get("task") or "").strip()
        if not task:
            self._json(400, {"ok": False, "error": "task required"})
            return
        if not _lock.acquire(blocking=False):
            # Informative busy — multi-caller coordination (a contending pillar sees who's
            # running + how long, and can retry). A real task QUEUE is adoption-time (YAGNI).
            with _state_lock:
                cur = dict(_current) if _current else {}
            busy = {"ok": False, "error": "busy — a task is already running"}
            if cur:
                busy["current"] = {"caller": cur.get("caller"), "task": str(cur.get("task"))[:140],
                                   "elapsed_s": int(time.time() - cur["started"]), "wall_s": cur.get("wall")}
            self._json(409, busy)
            return

        worker_owns_lock = False
        try:
            if not _rate_ok():
                self._json(429, {"ok": False, "error": f"rate limit — max {MAX_RUNS_PER_HOUR} runs/hour"})
                return
            from agent_runner import run_task
            mode = body.get("mode", "headless")
            cdp = "http://127.0.0.1:9222" if mode == "comet" else None
            channel = body.get("channel", "telegram")
            steps = int(body.get("steps", 12))
            vision = bool(body.get("vision", False))
            # who's calling (a pillar, Dove, the panel) — for telemetry attribution.
            caller = (body.get("caller") or "").strip() or ("panel" if channel == "panel" else "api")

            # --- async PANEL path: return a job id, run in a worker, approve inline ---
            if channel == "panel":
                from panel_jobs import new_job
                wall = max(MIN_WALL, min(PANEL_MAX_WALL, float(body.get("wall", 900))))
                job = new_job(task)
                with _state_lock:
                    _current = {"task": task, "started": time.time(), "wall": wall,
                                "label": "panel", "caller": caller}
                    _stuck = False

                def _worker():
                    global _current, _stuck
                    try:
                        rec = asyncio.run(run_task(
                            task, label="panel", caller=caller, max_steps=steps, max_wall_s=wall,
                            use_vision=vision, cdp_url=cdp,
                            approve=job.make_approver(PANEL_APPROVE_TIMEOUT),
                            persist=True, on_step=job.on_step))
                        job.result = rec
                        job.status = "done" if rec.get("status") == "ok" else "error"
                        job.add_event("done", status=rec.get("status"), final=rec.get("final"),
                                      steps=rec.get("steps"), dur_s=rec.get("dur_s"), err=rec.get("err"))
                    except Exception as e:  # noqa: BLE001
                        job.status = "error"
                        job.add_event("error", detail=str(e)[:300])
                    finally:
                        with _state_lock:
                            _current = None
                            _stuck = False
                        _lock.release()

                job.add_event("started", task=task[:200], mode=mode, vision=vision, wall=wall, caller=caller)
                threading.Thread(target=_worker, daemon=True).start()
                worker_owns_lock = True
                self._json(202, {"ok": True, "job_id": job.id})
                return

            # --- synchronous path (telegram / console / none): caller blocks ---
            approve = _make_approver(channel)
            wall = max(MIN_WALL, min(MAX_WALL, float(body.get("wall", 360))))
            with _state_lock:
                _current = {"task": task, "started": time.time(), "wall": wall,
                            "label": "server", "caller": caller}
                _stuck = False
            rec = asyncio.run(run_task(
                task, label="server", caller=caller, max_steps=steps, max_wall_s=wall,
                use_vision=vision, cdp_url=cdp, approve=approve, persist=True))
            self._json(200 if rec.get("status") == "ok" else 502, rec)
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal", "detail": str(e)[:300]})
        finally:
            if not worker_owns_lock:
                with _state_lock:
                    _current = None
                    _stuck = False
                _lock.release()

    def log_message(self, *a):
        pass


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    print(f"browser-assistant surface on http://{HOST}:{PORT} "
          f"(auth={'ON' if _TOKEN else 'OFF — set COMET_BRIDGE_TOKEN'}; "
          f"rate={MAX_RUNS_PER_HOUR}/h; wall<= {int(MAX_WALL)}s; reaper on)", flush=True)
    # Warm the heavy browser_use import so the FIRST /run responds instantly
    # (otherwise the lazy `from agent_runner import run_task` blocks the first request).
    try:
        import agent_runner  # noqa: F401
        print("warmed agent_runner (browser_use loaded)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"warm-import warning: {e}", flush=True)
    threading.Thread(target=_reaper, daemon=True).start()
    _Server((HOST, PORT), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    main()
