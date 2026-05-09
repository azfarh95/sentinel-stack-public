"""
Sentinel Mini App v2 Bridge — port 8098
Auth: Telegram identity → TOTP → session token
"""

import glob
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import keyring
import pyotp
import qrcode

from flask import Flask, jsonify, request, send_from_directory, Response

app = Flask(__name__, static_folder="static")

# ── Config ────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

_cfg = _load_config()

_MINIAPP_SERVICE = "sentinel-miniapp"


def _secret(key: str, env_var: str = "", cfg_key: str = "") -> str:
    """Load secret: Credential Manager → environment variable → config.json."""
    try:
        val = keyring.get_password(_MINIAPP_SERVICE, key)
        if val:
            return val
    except Exception:
        pass
    if env_var:
        val = os.environ.get(env_var, "")
        if val:
            return val
    return _cfg.get(cfg_key or key, "")


# ── Constants ─────────────────────────────────────────────────────────────────
VERSION_FILE      = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
OPENCLAW_JSON     = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\openclaw.json"
MODELS_JSON       = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\agent\models.json"
SESSIONS_DIR      = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions"
SHORTCUTS_JSON    = os.path.join(os.path.dirname(__file__), "shortcuts.json")
INFER_BRIDGE      = "http://127.0.0.1:8095/infer_status"
WATCHDOG_URL      = "http://127.0.0.1:8099"
MEMORY_MCP_URL    = "http://127.0.0.1:8092/mcp"
REMINDERS_MCP_URL = "http://127.0.0.1:8087/mcp"
CONTEXT_TOKENS    = 131072
SCRIPTS_DIR       = r"C:\Users\azfar\metamcp-local\scripts"

CHAT_IDS        = _cfg.get("telegram_chat_ids") or {"dm": "YOUR_TELEGRAM_CHAT_ID", "group": "-1003748374568"}
TELEGRAM_TOKEN  = _secret("telegram_bot_token", "TELEGRAM_BOT_TOKEN", "telegram_bot_token")
MINI_APP_URL    = _cfg.get("mini_app_url") or "https://your-domain.example.com"
MINI_APP_SECRET = _secret("mini_app_secret",    "MINI_APP_SECRET",    "mini_app_secret")
TOTP_SECRET     = _secret("totp_secret",        "TOTP_SECRET",        "totp_secret")
OWNER_ID        = int(CHAT_IDS.get("dm", "YOUR_TELEGRAM_CHAT_ID"))
BOT_USERNAME    = "YourSentinelBot"

# ── Restart maps ──────────────────────────────────────────────────────────────
_DOCKER_NAMES: dict[str, str] = {
    "MetaMCP":          "metamcp",
    "Reminders MCP":    "reminders-mcp",
    "yt-dlp MCP":       "ytdlp-mcp",
    "Google WS MCP":    "google-workspace-mcp",
    "Maps MCP":         "maps-mcp",
    "Memory MCP":       "memory-mcp",
    "GitHub MCP":       "github-mcp",
    "OneDrive MCP":     "onedrive-mcp",
    "Translate MCP":    "translate-mcp",
    "Nanobot (smdl)":   "smdl",
}
_DOCKER_PORTS: dict[str, int] = {
    "metamcp":               12008,
    "reminders-mcp":         8087,
    "ytdlp-mcp":             8088,
    "google-workspace-mcp":  8089,
    "maps-mcp":              8090,
    "github-mcp":            8091,
    "memory-mcp":            8092,
    "onedrive-mcp":          8093,
    "translate-mcp":         8094,
    "smdl":                  8096,
}

SERVICES = [
    {"name": "MetaMCP",              "port": 12008},
    {"name": "Sentinel (OpenClaw)",  "port": 18789},
    {"name": "Memory MCP",           "port": 8092},
    {"name": "Google WS MCP",        "port": 8089},
    {"name": "Reminders MCP",        "port": 8087},
    {"name": "Maps MCP",             "port": 8090},
    {"name": "GitHub MCP",           "port": 8091},
    {"name": "OneDrive MCP",         "port": 8093},
    {"name": "Translate MCP",        "port": 8094},
    {"name": "yt-dlp MCP",           "port": 8088},
    {"name": "Nanobot (smdl)",       "port": 8096},
    {"name": "Infer Bridge",         "port": 8095},
    {"name": "Sentinel Bridge",      "port": 8098},
    {"name": "LM Studio",            "port": 1234},
]

# ── Session / auth state ──────────────────────────────────────────────────────
# Tier 1: tg_tokens — ephemeral pre-TOTP tokens (5 min, in-memory only)
# Tier 2: sessions  — persistent SQLite + row-level HMAC integrity
#   Token: 256-bit random (secrets.token_bytes), only SHA-256 hash stored
#   Row MAC: HMAC-SHA256(token_hash|tg_id|ip|ua|created_at|expires_at, server_secret)
#   Direct DB edits (extend TTL, swap tg_id) are detected via MAC mismatch

TG_TOKEN_TTL  = 5 * 60
SESSION_TTL   = 8 * 3600

_tg_tokens:  dict[str, dict] = {}
_state_lock = threading.Lock()

# Rate limiting: {ip: [timestamp, ...]}
_rate: dict[str, list] = {}
RATE_WINDOW   = 15 * 60
RATE_MAX_FAIL = 5


def _rate_check(ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = time.time()
    with _state_lock:
        hits = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
        _rate[ip] = hits
        if len(hits) >= RATE_MAX_FAIL:
            return False
    return True


def _rate_fail(ip: str):
    now = time.time()
    with _state_lock:
        _rate.setdefault(ip, []).append(now)


def _rate_clear(ip: str):
    with _state_lock:
        _rate.pop(ip, None)


def _issue_tg_token(tg_id: int) -> str:
    token = secrets.token_hex(24)
    with _state_lock:
        _tg_tokens[token] = {"expires_at": time.time() + TG_TOKEN_TTL, "tg_id": tg_id}
    return token


def _consume_tg_token(token: str) -> int | None:
    """Validate and consume a tg_token. Returns tg_id or None."""
    with _state_lock:
        s = _tg_tokens.pop(token, None)
    if s and time.time() < s["expires_at"]:
        return s["tg_id"]
    return None


# ── SQLite session store ──────────────────────────────────────────────────────
_SESSION_DB          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")
_SESSION_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".session_secret")
_sess_local          = threading.local()
_sess_wlock          = threading.Lock()


def _sess_secret() -> bytes:
    if os.path.exists(_SESSION_SECRET_FILE):
        with open(_SESSION_SECRET_FILE, "rb") as f:
            return f.read()
    raw = secrets.token_bytes(32)
    with open(_SESSION_SECRET_FILE, "wb") as f:
        f.write(raw)
    try:
        os.chmod(_SESSION_SECRET_FILE, 0o600)
    except OSError:
        pass
    return raw


_SESS_SECRET = _sess_secret()


def _sess_conn() -> sqlite3.Connection:
    if not hasattr(_sess_local, "c"):
        c = sqlite3.connect(_SESSION_DB, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                tg_id      INTEGER NOT NULL,
                ip         TEXT    NOT NULL DEFAULT '',
                ua         TEXT    NOT NULL DEFAULT '',
                created_at REAL    NOT NULL,
                expires_at REAL    NOT NULL,
                row_mac    TEXT    NOT NULL
            )
        """)
        c.commit()
        _sess_local.c = c
    return _sess_local.c


def _sess_mac(token_hash: str, tg_id: int, ip: str, ua: str,
              created_at: float, expires_at: float) -> str:
    msg = f"{token_hash}|{tg_id}|{ip}|{ua}|{created_at}|{expires_at}".encode()
    return hmac.new(_SESS_SECRET, msg, hashlib.sha256).hexdigest()


def _new_session(tg_id: int, ip: str = "", ua: str = "") -> tuple[str, float]:
    raw = secrets.token_bytes(32)
    token = raw.hex()
    token_hash = hashlib.sha256(raw).hexdigest()
    now, exp = time.time(), time.time() + SESSION_TTL
    mac = _sess_mac(token_hash, tg_id, ip, ua, now, exp)
    with _sess_wlock:
        _sess_conn().execute(
            "INSERT INTO sessions (token_hash, tg_id, ip, ua, created_at, expires_at, row_mac) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_hash, tg_id, ip, ua, now, exp, mac),
        )
        _sess_conn().commit()
    return token, exp


def _valid_session(token: str) -> bool:
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return False
    token_hash = hashlib.sha256(raw).hexdigest()
    row = _sess_conn().execute(
        "SELECT tg_id, ip, ua, created_at, expires_at, row_mac FROM sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if not row:
        return False
    tg_id, ip, ua, created_at, expires_at, stored_mac = row
    if not hmac.compare_digest(_sess_mac(token_hash, tg_id, ip, ua, created_at, expires_at), stored_mac):
        return False
    return time.time() < expires_at


def _session_info(token: str) -> dict | None:
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return None
    token_hash = hashlib.sha256(raw).hexdigest()
    row = _sess_conn().execute(
        "SELECT tg_id, ip, ua, created_at, expires_at, row_mac FROM sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    tg_id, ip, ua, created_at, expires_at, stored_mac = row
    if not hmac.compare_digest(_sess_mac(token_hash, tg_id, ip, ua, created_at, expires_at), stored_mac):
        return None
    if time.time() >= expires_at:
        return None
    return {"tg_id": tg_id, "expires_at": expires_at, "created_at": created_at, "ip": ip, "ua": ua}


def _session_list() -> list[dict]:
    now = time.time()
    rows = _sess_conn().execute(
        "SELECT token_hash, tg_id, ip, ua, created_at, expires_at, row_mac "
        "FROM sessions WHERE expires_at > ? ORDER BY created_at DESC",
        (now,),
    ).fetchall()
    result = []
    for token_hash, tg_id, ip, ua, created_at, expires_at, stored_mac in rows:
        if not hmac.compare_digest(_sess_mac(token_hash, tg_id, ip, ua, created_at, expires_at), stored_mac):
            continue
        result.append({
            "id": token_hash[:8],
            "token": token_hash[:8],
            "tg_id": tg_id,
            "ip": ip,
            "ua": ua,
            "created_at": created_at,
            "expires_at": expires_at,
        })
    return result


def _revoke_session(token_or_prefix: str) -> bool:
    with _sess_wlock:
        try:
            raw = bytes.fromhex(token_or_prefix)
            token_hash = hashlib.sha256(raw).hexdigest()
            cur = _sess_conn().execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            _sess_conn().commit()
            if cur.rowcount:
                return True
        except ValueError:
            pass
        cur = _sess_conn().execute(
            "DELETE FROM sessions WHERE token_hash LIKE ?",
            (token_or_prefix[:8] + "%",),
        )
        _sess_conn().commit()
        return cur.rowcount > 0


def _purge_expired():
    with _sess_wlock:
        _sess_conn().execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
        _sess_conn().commit()


# ── Telegram auth verification ────────────────────────────────────────────────
def _verify_initdata(init_data: str) -> int | None:
    """Verify Telegram Mini App initData. Returns Telegram user ID or None."""
    try:
        params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        check_hash = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, check_hash):
            return None
        if time.time() - int(params.get("auth_date", 0)) > 86400:
            return None
        user = json.loads(params.get("user", "{}"))
        return int(user.get("id", 0)) or None
    except Exception:
        return None


def _verify_widget(data: dict) -> int | None:
    """Verify Telegram Login Widget auth data. Returns Telegram user ID or None."""
    try:
        data = dict(data)
        check_hash = data.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hashlib.sha256(TELEGRAM_TOKEN.encode()).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, check_hash):
            return None
        if time.time() - int(data.get("auth_date", 0)) > 300:
            return None
        return int(data.get("id", 0)) or None
    except Exception:
        return None


# ── TOTP setup page (local only) ──────────────────────────────────────────────
def _write_totp_setup_page():
    if not TOTP_SECRET:
        return
    uri = pyotp.TOTP(TOTP_SECRET).provisioning_uri(name="azfar", issuer_name="Sentinel")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    import base64
    b64 = base64.b64encode(buf.getvalue()).decode()
    out = os.path.join(os.path.dirname(__file__), "totp_setup.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Sentinel TOTP Setup</title>
<style>body{{font-family:system-ui;max-width:420px;margin:60px auto;text-align:center;background:#111;color:#eee}}
img{{border-radius:12px;margin:24px 0}}code{{background:#222;padding:8px 16px;border-radius:8px;letter-spacing:2px;font-size:16px}}
p{{color:#888;font-size:13px;margin-top:32px}}</style></head>
<body>
<h2>⚡ Sentinel — Authenticator Setup</h2>
<p style="color:#aaa">Scan with Google Authenticator</p>
<img src="data:image/png;base64,{b64}" width="220" height="220" />
<br>Or enter manually:<br><br>
<code>{TOTP_SECRET}</code>
<p>This file is local-only and never served over the web.<br>Delete it after setup if you prefer.</p>
</body></html>""")
    print(f"[sentinel-v2] TOTP setup page -> {out}")
    print(f"[sentinel-v2] TOTP secret: {TOTP_SECRET}")


# ── MCP Clients (identical to v1) ────────────────────────────────────────────
class _MCPClient:
    def __init__(self, url: str, name: str):
        self.url, self.name = url, name
        self._session_id: str | None = None
        self._lock = threading.Lock()

    def _ensure_session(self) -> str:
        with self._lock:
            if self._session_id:
                return self._session_id
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "sentinel-bridge-v2", "version": "2.0"}}
            }).encode()
            req = urllib.request.Request(self.url, data=payload, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                sid = r.headers.get("mcp-session-id", "")
            if not sid:
                raise RuntimeError(f"No session ID from {self.name}")
            self._session_id = sid
            return sid

    def _call(self, tool: str, args: dict):
        for attempt in range(2):
            sid = self._ensure_session()
            payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool, "arguments": args}}).encode()
            req = urllib.request.Request(self.url, data=payload, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "mcp-session-id": sid
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read().decode()
                for line in raw.splitlines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        content = data.get("result", {}).get("content", [])
                        if not content:
                            return {}
                        if len(content) == 1:
                            text = content[0].get("text", "{}")
                            try:
                                return json.loads(text)
                            except json.JSONDecodeError:
                                return {"raw": text}
                        result = []
                        for item in content:
                            try:
                                result.append(json.loads(item.get("text", "{}")))
                            except json.JSONDecodeError:
                                pass
                        return result
                return {}
            except (urllib.error.HTTPError, urllib.error.URLError):
                with self._lock:
                    self._session_id = None
                if attempt == 1:
                    raise


class MemoryMCPClient(_MCPClient):
    def __init__(self): super().__init__(MEMORY_MCP_URL, "memory-mcp")
    def stats(self): return self._call("memory_stats", {})
    def list_memories(self, limit=20, tags=None):
        r = self._call("memory_list", {"limit": limit, **({"tags": tags} if tags else {})})
        return r if isinstance(r, list) else r.get("result", [])
    def search(self, query, limit=10):
        r = self._call("memory_search", {"query": query, "limit": limit})
        return r if isinstance(r, list) else r.get("result", [])
    def store(self, content, tags=None, source=None):
        args = {"content": content}
        if tags:   args["tags"]   = tags
        if source: args["source"] = source
        return self._call("memory_store", args)
    def delete(self, memory_id): return self._call("memory_delete", {"memory_id": memory_id})


class RemindersMCPClient(_MCPClient):
    def __init__(self): super().__init__(REMINDERS_MCP_URL, "reminders-mcp")
    def list_all(self):
        r = self._call("list_reminders", {})
        return r if isinstance(r, list) else r.get("result", [])
    def add(self, chat_id, message, when, label="", recipients=None):
        args = {"chat_id": chat_id, "message": message, "when": when}
        if label:      args["label"]      = label
        if recipients: args["recipients"] = recipients
        return self._call("add_reminder", args)
    def cancel(self, reminder_id): return self._call("cancel_reminder", {"reminder_id": reminder_id})


memory          = MemoryMCPClient()
reminders_client = RemindersMCPClient()

# ── Playwright MCP client (V3 browser panel) ──────────────────────────────────
from playwright_client import PlaywrightMCPClient
_metamcp_token = _secret("metamcp_bearer_token", "METAMCP_BEARER_TOKEN", "metamcp_bearer_token")
playwright_client = PlaywrightMCPClient(token=_metamcp_token) if _metamcp_token else None


# ── Telegram helper ───────────────────────────────────────────────────────────
def _tg_post(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())




# ── Helpers ───────────────────────────────────────────────────────────────────
def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False

def get_inference_status(force: bool = False):
    url = INFER_BRIDGE + ("?force=1" if force else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return {"active": False, "model": "unknown", "error": "unreachable"}

def get_context_estimate():
    try:
        pattern = os.path.join(SESSIONS_DIR, "*.jsonl")
        all_files = glob.glob(pattern)
        suffixes = (".deleted", ".reset", ".bak", ".tmp")
        active = [f for f in all_files
                  if not any(s in os.path.basename(f) for s in suffixes)
                  and f.endswith(".jsonl")
                  and ".trajectory." not in os.path.basename(f)
                  and ".checkpoint." not in os.path.basename(f)]
        if not active:
            return {"tokens": 0, "pct": 0}
        current = max(active, key=os.path.getmtime)
        tokens  = os.path.getsize(current) // 4
        return {"tokens": tokens, "pct": min(100, round(tokens / CONTEXT_TOKENS * 100))}
    except Exception:
        return {"tokens": 0, "pct": 0}

def _port_up(port, timeout=1.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False

def _openclaw_up():
    if _port_up(18789):
        return True
    try:
        r = subprocess.run(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
                            "ss -tlnp 2>/dev/null | grep -q ':18789'"],
                           timeout=5, capture_output=True)
        return r.returncode == 0
    except Exception:
        return False

def _client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "")
            .split(",")[0].strip())


# ── CORS ──────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Sentinel-Token, X-Session-Token"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return r

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return jsonify({})


# ── Auth middleware ───────────────────────────────────────────────────────────
# Pre-TOTP routes — only need X-Sentinel-Token (embedded in page)
_PRE_TOTP  = {"/api/auth/telegram", "/api/auth/verify", "/api/auth/status"}
# Exempt — called locally by OpenClaw without any token
_EXEMPT    = {"/api/send-dashboard"}

@app.before_request
def check_auth():
    if not request.path.startswith("/api/"):
        return
    if request.method == "OPTIONS":
        return
    if request.path in _EXEMPT:
        return
    if not MINI_APP_SECRET:
        return  # dev mode

    sentinel = (request.headers.get("X-Sentinel-Token", "")
                or request.args.get("token", ""))

    if request.path in _PRE_TOTP:
        if sentinel != MINI_APP_SECRET:
            return jsonify({"error": "unauthorized"}), 401
        return

    # Session token: prefer header (standard), accept query for SSE/EventSource
    # which can't set custom headers. EventSource still goes through this gate.
    session_token = (request.headers.get("X-Session-Token", "")
                     or request.args.get("session", ""))
    if not _valid_session(session_token):
        return jsonify({"error": "session_required"}), 401


# ── API: Auth ─────────────────────────────────────────────────────────────────
@app.route("/api/auth/telegram", methods=["POST"])
def api_auth_telegram():
    ip = _client_ip()
    if not _rate_check(ip):
        return jsonify({"error": "too_many_attempts",
                        "retry_after": RATE_WINDOW}), 429

    data     = request.json or {}
    tg_id    = None
    method   = data.get("method")           # "initdata" | "widget"

    if method == "initdata":
        tg_id = _verify_initdata(data.get("init_data", ""))
    elif method == "widget":
        tg_id = _verify_widget(data.get("auth_data", {}))

    if not tg_id:
        _rate_fail(ip)
        return jsonify({"error": "invalid_auth"}), 401

    if tg_id != OWNER_ID:
        _rate_fail(ip)
        return jsonify({"error": "access_denied"}), 403

    _rate_clear(ip)
    tg_token = _issue_tg_token(tg_id)
    return jsonify({"ok": True, "tg_token": tg_token,
                    "expires_in": TG_TOKEN_TTL})


@app.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    ip = _client_ip()
    if not _rate_check(ip):
        return jsonify({"error": "too_many_attempts", "retry_after": RATE_WINDOW}), 429

    data     = request.json or {}
    tg_token = data.get("tg_token", "")
    code     = str(data.get("code", "")).strip().replace(" ", "")

    tg_id = _consume_tg_token(tg_token)
    if not tg_id:
        _rate_fail(ip)
        return jsonify({"error": "tg_token_invalid"}), 401

    if TOTP_SECRET:
        if not pyotp.TOTP(TOTP_SECRET).verify(code, valid_window=1):
            _rate_fail(ip)
            return jsonify({"error": "invalid_code"}), 401

    _rate_clear(ip)
    ua  = request.headers.get("User-Agent", "")[:120]
    tok, exp = _new_session(tg_id, ip, ua)
    return jsonify({"ok": True, "session_token": tok, "expires_at": exp})


@app.route("/api/auth/status")
def api_auth_status():
    session_tok = request.headers.get("X-Session-Token", "")
    valid = _valid_session(session_tok)
    exp   = None
    if valid:
        info = _session_info(session_tok)
        exp  = info.get("expires_at") if info else None
    return jsonify({"authenticated": valid, "expires_at": exp})


@app.route("/api/auth/sessions")
def api_auth_sessions():
    return jsonify(_session_list())


@app.route("/api/auth/sessions/<token_id>", methods=["DELETE"])
def api_auth_sessions_revoke(token_id):
    ok = _revoke_session(token_id)
    return jsonify({"ok": ok}) if ok else (jsonify({"error": "not found"}), 404)


# ── API: Version ─────────────────────────────────────────────────────────────
@app.route("/api/version")
def api_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            version = f.read().strip()
    except Exception:
        version = "unknown"
    return jsonify({"version": version, "tag": f"v{version}"})


# ── API: Status ───────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    cfg   = read_json(OPENCLAW_JSON)
    model = cfg.get("agents",{}).get("defaults",{}).get("model",{}).get("primary","unknown")
    infer = get_inference_status()
    ctx   = get_context_estimate()
    try:
        stats     = memory.stats()
        mem_count = stats.get("total_memories", 0)
        last_mem  = stats.get("newest")
    except Exception:
        mem_count, last_mem = 0, None
    return jsonify({"model": model, "inference_active": infer.get("active", False),
                    "inference_model": infer.get("model"), "memory_count": mem_count,
                    "last_memory_at": last_mem, "context_tokens": ctx["tokens"],
                    "context_pct": ctx["pct"]})


# ── API: Memories ─────────────────────────────────────────────────────────────
@app.route("/api/memories")
def api_memories_list():
    limit = int(request.args.get("limit", 20))
    tags  = request.args.getlist("tags") or None
    q     = request.args.get("q", "").strip()
    try:
        result = memory.search(q, limit) if q else memory.list_memories(limit, tags)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/memories", methods=["POST"])
def api_memories_store():
    data    = request.json or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    try:
        return jsonify(memory.store(content, data.get("tags"), data.get("source", "miniapp")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/memories/<int:memory_id>", methods=["DELETE"])
def api_memories_delete(memory_id):
    try:
        return jsonify(memory.delete(memory_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: Shortcuts ────────────────────────────────────────────────────────────
@app.route("/api/shortcuts")
def api_shortcuts():
    return jsonify(read_json(SHORTCUTS_JSON).get("shortcuts", []))


# ── API: Models ───────────────────────────────────────────────────────────────

# Curated OpenRouter model presets — free tier first, then frontier paid models.
OPENROUTER_PRESETS = [
    {"id": "openrouter/free",                           "name": "OpenRouter Auto (Free)"},
    {"id": "deepseek/deepseek-chat-v3:free",            "name": "DeepSeek V3 (Free)"},
    {"id": "meta-llama/llama-3.3-70b-instruct:free",    "name": "Llama 3.3 70B (Free)"},
    {"id": "google/gemini-2.0-flash-exp:free",          "name": "Gemini 2.0 Flash (Free)"},
    {"id": "google/gemma-3-27b-it:free",                "name": "Gemma 3 27B (Free)"},
    {"id": "qwen/qwen-2.5-72b-instruct:free",           "name": "Qwen 2.5 72B (Free)"},
    {"id": "anthropic/claude-sonnet-4.5",               "name": "Claude Sonnet 4.5 (Paid)"},
    {"id": "openai/gpt-4o-mini",                        "name": "GPT-4o mini (Paid)"},
    {"id": "google/gemini-2.5-pro",                     "name": "Gemini 2.5 Pro (Paid)"},
]


_AUTH_PROFILES_JSON = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\agent\auth-profiles.json"


def _read_openclaw_openrouter_key() -> str:
    """Read the OpenRouter key from OpenClaw's auth-profiles.json (canonical OpenClaw location)."""
    try:
        with open(_AUTH_PROFILES_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("profiles", {})
                    .get("openrouter:default", {})
                    .get("key", "") or "")
    except Exception:
        return ""


def _get_existing_openrouter_key() -> str:
    """Discover an existing OpenRouter key from any source. WCM is canonical for new keys,
    but on first use we may find one already in OpenClaw's auth-profiles."""
    try:
        wcm = keyring.get_password(_MINIAPP_SERVICE, "openrouter_api_key")
        if wcm:
            return wcm
    except Exception:
        pass
    return _read_openclaw_openrouter_key()


def _has_openrouter_key() -> bool:
    return bool(_get_existing_openrouter_key())


@app.route("/api/models")
def api_models():
    cfg     = read_json(OPENCLAW_JSON)
    primary = cfg.get("agents",{}).get("defaults",{}).get("model",{}).get("primary","")
    models  = []
    for pid, prov in cfg.get("models",{}).get("providers",{}).items():
        for m in prov.get("models", []):
            fid = f"{pid}/{m['id']}"
            models.append({"id": fid, "name": m.get("name", m["id"]),
                           "provider": pid, "active": fid == primary})
    return jsonify({
        "models":             models,
        "has_openrouter_key": _has_openrouter_key(),
        "openrouter_presets": OPENROUTER_PRESETS,
    })


@app.route("/api/models/active", methods=["POST"])
def api_models_switch():
    data     = request.json or {}
    model_id = data.get("model_id", "").strip()
    if not model_id:
        return jsonify({"error": "model_id required"}), 400
    cfg = read_json(OPENCLAW_JSON)
    cfg.setdefault("agents",{}).setdefault("defaults",{}).setdefault("model",{})["primary"] = model_id
    if not write_json(OPENCLAW_JSON, cfg):
        return jsonify({"error": "failed to write config"}), 500
    try:
        subprocess.run(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
                        "systemctl kill -s SIGUSR1 openclaw-gateway.service"],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    return jsonify({"ok": True, "model_id": model_id})


@app.route("/api/models/openrouter/add", methods=["POST"])
def api_models_openrouter_add():
    """Add an OpenRouter model. Stores API key in Windows Credential Manager,
    mirrors into openclaw.json so the gateway can use it."""
    data     = request.json or {}
    model_id = data.get("model_id", "").strip()
    name     = (data.get("name") or model_id).strip()
    api_key  = (data.get("api_key") or "").strip()
    if not model_id:
        return jsonify({"error": "model_id required"}), 400

    # Persist API key. If submitted, save it. If not, discover from any source
    # (WCM canonical, OpenClaw auth-profiles fallback) and mirror to WCM.
    if api_key:
        try:
            keyring.set_password(_MINIAPP_SERVICE, "openrouter_api_key", api_key)
        except Exception as e:
            return jsonify({"error": f"could not save key: {e}"}), 500
    else:
        api_key = _get_existing_openrouter_key()
        if api_key:
            try:
                keyring.set_password(_MINIAPP_SERVICE, "openrouter_api_key", api_key)
            except Exception:
                pass  # not fatal — we still have the key in memory
    if not api_key:
        return jsonify({"error": "no API key on file — provide one"}), 400

    cfg = read_json(OPENCLAW_JSON)
    providers = cfg.setdefault("models", {}).setdefault("providers", {})
    prov = providers.setdefault("openrouter", {
        "baseUrl": "https://openrouter.ai/api/v1",
        "api":     "openai-completions",
        "auth":    "api-key",
        "models":  [],
        "timeoutSeconds": 600,
    })
    prov["apiKey"] = api_key   # mirror from WCM each time (canonical source = WCM)

    existing_ids = {m.get("id") for m in prov.get("models", [])}
    if model_id not in existing_ids:
        prov.setdefault("models", []).append({
            "id":            model_id,
            "name":          name,
            "reasoning":     False,
            "input":         ["text"],
            "cost":          {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 131072,
            "contextTokens": 65536,
            "maxTokens":     4096,
        })

    if not write_json(OPENCLAW_JSON, cfg):
        return jsonify({"error": "failed to write config"}), 500
    return jsonify({"ok": True, "model_id": f"openrouter/{model_id}"})


@app.route("/api/models/<path:full_id>", methods=["DELETE"])
def api_models_remove(full_id):
    """Remove a model. full_id like 'openrouter/deepseek/deepseek-chat-v3:free'."""
    if "/" not in full_id:
        return jsonify({"error": "invalid id"}), 400
    pid, model_id = full_id.split("/", 1)
    cfg = read_json(OPENCLAW_JSON)
    prov = cfg.get("models", {}).get("providers", {}).get(pid)
    if not prov:
        return jsonify({"error": "provider not found"}), 404
    before = len(prov.get("models", []))
    prov["models"] = [m for m in prov.get("models", []) if m.get("id") != model_id]
    if len(prov["models"]) == before:
        return jsonify({"error": "model not found"}), 404
    if not write_json(OPENCLAW_JSON, cfg):
        return jsonify({"error": "failed to write config"}), 500
    return jsonify({"ok": True})


# ── API: Inference ────────────────────────────────────────────────────────────
@app.route("/api/inference/status")
def api_inference_status():
    force = request.args.get("force") == "1"
    return jsonify(get_inference_status(force=force))

@app.route("/api/inference/restart", methods=["POST"])
def api_inference_restart():
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if ":8095 " in line and "LISTENING" in line:
                pid = line.split()[-1]
                subprocess.run(["taskkill", "/PID", pid, "/F"], timeout=3, capture_output=True)
                break
        time.sleep(1)
        subprocess.Popen(["py", "-3", r"C:\Users\azfar\metamcp-local\infer_bridge.py"],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: Services ─────────────────────────────────────────────────────────────
def _watchdog_status() -> dict | None:
    """Fetch full status from watchdog. Returns None on failure."""
    try:
        with urllib.request.urlopen(f"{WATCHDOG_URL}/status", timeout=5) as r:
            data = json.loads(r.read())
        port_map = {s["name"]: s["port"] for s in SERVICES}
        services = [
            {"name": name, "port": port_map.get(name), "healthy": bool(ok)}
            for name, ok in data.get("services", {}).items()
        ]
        endpoints = [
            {"name": name, "ok": bool(info.get("ok")), "detail": info.get("detail", "")}
            for name, info in data.get("endpoints", {}).items()
        ]
        return {
            "services":         services,
            "endpoints":        endpoints,
            "oc_dupe_conflict": data.get("oc_dupe_conflict", False),
            "contacts":         data.get("contacts", []),
        } if services else None
    except Exception:
        return None


@app.route("/api/services")
def api_services():
    watchdog = _watchdog_status()
    if watchdog:
        return jsonify({**watchdog, "source": "watchdog"})
    # Fallback: direct port checks, no endpoint detail available
    regular = [s for s in SERVICES if s["port"] != 18789]
    with ThreadPoolExecutor(max_workers=12) as pool:
        port_futs = {pool.submit(_port_up, s["port"]): s for s in regular}
        oc_fut    = pool.submit(_openclaw_up)
        results   = [{"name": s["name"], "port": s["port"], "healthy": f.result()}
                     for f, s in port_futs.items()]
    results.insert(1, {"name": "OpenClaw", "port": 18789, "healthy": oc_fut.result()})
    return jsonify({"services": results, "endpoints": [], "source": "direct"})


# ── API: Updates ──────────────────────────────────────────────────────────────

@app.route("/api/updates")
def api_updates():
    try:
        with urllib.request.urlopen(f"{WATCHDOG_URL}/versions", timeout=30) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/updates/run", methods=["POST"])
def api_updates_run():
    data      = request.json or {}
    update_id = data.get("update_id", "")
    payload   = json.dumps({"update_id": update_id}).encode()
    try:
        req = urllib.request.Request(
            f"{WATCHDOG_URL}/update",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ── API: Service restart ──────────────────────────────────────────────────────
def _send_critical_alert(name: str):
    try:
        _tg_post("sendMessage", {
            "chat_id": OWNER_ID,
            "text": (f"\U0001f6a8 *CRITICAL: {name}*\n"
                     f"Failed to recover after automatic restart attempt.\n"
                     f"Manual intervention required."),
            "parse_mode": "Markdown",
        })
    except Exception:
        pass


@app.route("/api/service/restart", methods=["POST"])
def api_service_restart():
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    ok = False

    if name in _DOCKER_NAMES:
        container = _DOCKER_NAMES[name]
        try:
            r = subprocess.run(["docker", "restart", container],
                               timeout=30, capture_output=True)
            if r.returncode == 0:
                time.sleep(4)
                port = _DOCKER_PORTS.get(container)
                ok = _port_up(port) if port else True
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    elif name in ("Sentinel (OpenClaw)", "OpenClaw"):
        try:
            svc = "openclaw-gateway.service"
            subprocess.run(
                ["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
                 f"systemctl reset-failed {svc}; systemctl restart {svc}"],
                timeout=35, capture_output=True,
            )
            time.sleep(4)
            ok = _openclaw_up()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    elif name == "Infer Bridge":
        return api_inference_restart()

    else:
        return jsonify({"error": f"no restart handler for '{name}'"}), 400

    if not ok:
        _send_critical_alert(name)
        return jsonify({"ok": False, "critical": True})

    return jsonify({"ok": True})


# ── API: OpenClaw Config ──────────────────────────────────────────────────────
@app.route("/api/openclaw/config")
def api_openclaw_config_get():
    cfg     = read_json(OPENCLAW_JSON)
    primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
    model_name = primary.split("/")[-1] if primary else "unknown"
    available  = ["none", "minimal", "low", "medium", "high", "xhigh"]
    lm_model   = {}
    for pid, prov in cfg.get("models", {}).get("providers", {}).items():
        for m in prov.get("models", []):
            if f"{pid}/{m['id']}" == primary:
                model_name = m.get("name", model_name)
                available  = m.get("compat", {}).get("supportedReasoningEfforts", available)
                lm_model   = m
                break
    lm_prov = cfg.get("models", {}).get("providers", {}).get("lmstudio", {})
    effort  = (cfg.get("agents", {}).get("defaults", {}).get("models", {})
               .get(primary, {}).get("reasoningEffort", "medium"))
    return jsonify({
        "primary":          primary,
        "model_name":       model_name,
        "reasoning_effort": effort,
        "available_efforts":available,
        "max_tokens":       lm_model.get("maxTokens", 8192),
        "context_tokens":   lm_model.get("contextTokens", 98304),
        "timeout_seconds":  lm_prov.get("timeoutSeconds", 600),
        "typing_interval":  cfg.get("agents", {}).get("defaults", {}).get("typingIntervalSeconds", 3),
        "web_search":       cfg.get("tools", {}).get("web", {}).get("search", {}).get("enabled", True),
        "web_fetch":        cfg.get("tools", {}).get("web", {}).get("fetch", {}).get("enabled", True),
    })


@app.route("/api/openclaw/config", methods=["POST"])
def api_openclaw_config_set():
    data    = request.json or {}
    cfg     = read_json(OPENCLAW_JSON)
    primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
    if not primary:
        return jsonify({"error": "no primary model set"}), 400

    # Reasoning effort
    effort = data.get("reasoning_effort", "").strip()
    if effort:
        available = ["none", "minimal", "low", "medium", "high", "xhigh"]
        for pid, prov in cfg.get("models", {}).get("providers", {}).items():
            for m in prov.get("models", []):
                if f"{pid}/{m['id']}" == primary:
                    available = m.get("compat", {}).get("supportedReasoningEfforts", available)
                    break
        if effort not in available:
            return jsonify({"error": f"invalid effort '{effort}'"}), 400
        overrides = cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
        overrides.setdefault(primary, {})["reasoningEffort"] = effort

    # Max tokens
    if "max_tokens" in data:
        lm_models = (cfg.setdefault("models", {}).setdefault("providers", {})
                        .setdefault("lmstudio", {}).setdefault("models", [{}]))
        if not lm_models:
            lm_models.append({})
        lm_models[0]["maxTokens"] = int(data["max_tokens"])

    # Timeout
    if "timeout_seconds" in data:
        (cfg.setdefault("models", {}).setdefault("providers", {})
            .setdefault("lmstudio", {}))["timeoutSeconds"] = int(data["timeout_seconds"])

    # Web search / fetch
    if "web_search" in data:
        (cfg.setdefault("tools", {}).setdefault("web", {})
            .setdefault("search", {}))["enabled"] = bool(data["web_search"])
    if "web_fetch" in data:
        (cfg.setdefault("tools", {}).setdefault("web", {})
            .setdefault("fetch", {}))["enabled"] = bool(data["web_fetch"])

    if not write_json(OPENCLAW_JSON, cfg):
        return jsonify({"error": "failed to write config"}), 500
    try:
        subprocess.run(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
                        "systemctl kill -s SIGUSR1 openclaw-gateway.service"],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    return jsonify({"ok": True})


# ── API: OpenClaw Skills ──────────────────────────────────────────────────────
@app.route("/api/openclaw/skills")
def api_openclaw_skills_get():
    cfg     = read_json(OPENCLAW_JSON)
    entries = cfg.get("skills", {}).get("entries", {})
    skills  = sorted(
        [{"name": name, "enabled": bool(info.get("enabled", False))}
         for name, info in entries.items()],
        key=lambda s: (not s["enabled"], s["name"]),
    )
    return jsonify(skills)


@app.route("/api/openclaw/skills", methods=["POST"])
def api_openclaw_skills_set():
    data    = request.json or {}
    updates = data.get("skills", {})
    if not isinstance(updates, dict):
        return jsonify({"error": "skills must be a dict"}), 400
    cfg     = read_json(OPENCLAW_JSON)
    entries = cfg.setdefault("skills", {}).setdefault("entries", {})
    for name, enabled in updates.items():
        entries.setdefault(name, {})["enabled"] = bool(enabled)
    if not write_json(OPENCLAW_JSON, cfg):
        return jsonify({"error": "failed to write config"}), 500
    try:
        subprocess.run(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
                        "systemctl kill -s SIGUSR1 openclaw-gateway.service"],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    return jsonify({"ok": True})


# ── API: Skill Credentials (Windows Credential Manager via keyring) ───────────
_SKILL_SVC    = "sentinel-skill"
_SKILL_KEYS_K = "__keys__"

def _skill_svc(name: str) -> str:
    return f"{_SKILL_SVC}-{name}"

def _skill_keys(name: str) -> list[str]:
    raw = keyring.get_password(_skill_svc(name), _SKILL_KEYS_K) or "[]"
    try:
        return json.loads(raw)
    except Exception:
        return []

def _skill_keys_save(name: str, keys: list[str]):
    keyring.set_password(_skill_svc(name), _SKILL_KEYS_K, json.dumps(keys))


@app.route("/api/openclaw/skills/<name>/credentials")
def api_skill_creds_get(name):
    keys   = _skill_keys(name)
    result = [{"key": k, "is_set": keyring.get_password(_skill_svc(name), k) is not None}
              for k in keys]
    return jsonify(result)


@app.route("/api/openclaw/skills/<name>/credentials", methods=["POST"])
def api_skill_creds_set(name):
    data  = request.json or {}
    key   = data.get("key", "").strip()
    value = data.get("value", "")
    if not key:
        return jsonify({"error": "key required"}), 400
    keys = _skill_keys(name)
    if key not in keys:
        keys.append(key)
        _skill_keys_save(name, keys)
    keyring.set_password(_skill_svc(name), key, value)
    return jsonify({"ok": True})


@app.route("/api/openclaw/skills/<name>/credentials/<key>", methods=["DELETE"])
def api_skill_creds_delete(name, key):
    try:
        keyring.delete_password(_skill_svc(name), key)
    except Exception:
        pass
    _skill_keys_save(name, [k for k in _skill_keys(name) if k != key])
    return jsonify({"ok": True})


# ── API: OpenClaw Doctor ──────────────────────────────────────────────────────
@app.route("/api/openclaw/doctor")
def api_openclaw_doctor():
    checks = []

    try:
        r = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
             "systemctl is-active openclaw-gateway.service"],
            timeout=8, capture_output=True, text=True,
        )
        state = r.stdout.strip()
        checks.append({"name": "systemd", "ok": state == "active", "detail": state or "unknown"})
    except Exception as e:
        checks.append({"name": "systemd", "ok": False, "detail": str(e)})

    oc_up = _openclaw_up()
    checks.append({"name": "OpenClaw :18789", "ok": oc_up,
                   "detail": "listening" if oc_up else "no response"})

    mm_up = _port_up(12008)
    checks.append({"name": "MetaMCP :12008", "ok": mm_up,
                   "detail": "reachable" if mm_up else "unreachable"})

    mem_up = _port_up(8092)
    checks.append({"name": "Memory MCP :8092", "ok": mem_up,
                   "detail": "reachable" if mem_up else "unreachable"})

    try:
        stats = memory.stats()
        cnt   = stats.get("total_memories", 0)
        checks.append({"name": "Memory", "ok": cnt > 0, "detail": f"{cnt} memories"})
    except Exception:
        checks.append({"name": "Memory", "ok": False, "detail": "unreachable"})

    logs = []
    try:
        r = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
             "journalctl -u openclaw-gateway.service -n 8 --no-pager --output=short 2>&1"],
            timeout=10, capture_output=True, text=True,
        )
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        logs  = lines[-8:]
    except Exception as e:
        logs = [str(e)]

    return jsonify({"checks": checks, "logs": logs})


# ── API: Stack ────────────────────────────────────────────────────────────────
@app.route("/api/stack/<action>", methods=["POST"])
def api_stack_action(action):
    scripts = {"stop":    os.path.join(SCRIPTS_DIR, "STOP_AI_STACK.bat"),
               "start":   os.path.join(SCRIPTS_DIR, "START_AI_STACK.bat"),
               "restart": os.path.join(SCRIPTS_DIR, "RESTART_AI_STACK.bat")}
    script = scripts.get(action)
    if not script:
        return jsonify({"error": f"unknown action: {action}"}), 400
    env = os.environ.copy()
    env["NOPAUSE"] = "1"
    subprocess.Popen(["cmd", "/c", script], env=env,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return jsonify({"ok": True, "action": action})


# ── API: Reminders ────────────────────────────────────────────────────────────
@app.route("/api/reminders")
def api_reminders_list():
    try:
        result = reminders_client.list_all()
        return jsonify(result if isinstance(result, list) else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/reminders", methods=["POST"])
def api_reminders_add():
    data        = request.json or {}
    message     = data.get("message", "").strip()
    when        = data.get("when", "").strip()
    label       = data.get("label", "").strip()
    target      = data.get("target", "dm")
    contact_ids = data.get("contact_ids", [])   # list of chat_id strings
    if not message or not when:
        return jsonify({"error": "message and when are required"}), 400
    if target == "contacts" and contact_ids:
        # Primary recipient is the first contact; extras go in recipients list
        chat_id    = str(contact_ids[0])
        recipients = [str(c) for c in contact_ids[1:]] or None
    else:
        chat_id    = CHAT_IDS.get(target, CHAT_IDS["dm"])
        recipients = None
    try:
        return jsonify(reminders_client.add(chat_id, message, when, label,
                                            recipients=recipients))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/reminders/<reminder_id>", methods=["DELETE"])
def api_reminders_cancel(reminder_id):
    try:
        return jsonify(reminders_client.cancel(reminder_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: Browser stream (V3 Phase 1.0 — active polling, shared context) ─────
# Playwright MCP is now launched with --shared-browser-context, so multiple
# HTTP clients (the agent + this bridge) can use the same browser concurrently
# without lock conflicts. We poll for screenshots actively at ~2 fps for live
# continuous view.
_browser_clients = 0
_browser_clients_lock = threading.Lock()
_browser_target_interval = 0.5  # seconds between captures (~2 fps)
_browser_last_jpeg = None
_browser_last_ts   = 0.0
_browser_last_path = ""


def _browser_capture_loop():
    """Active polling: while clients > 0, take a screenshot via Playwright MCP.
    --shared-browser-context means no contention with the agent."""
    global _browser_last_jpeg, _browser_last_ts, _browser_last_path
    while True:
        with _browser_clients_lock:
            n = _browser_clients
        if n == 0 or not playwright_client:
            time.sleep(1.0)
            continue
        try:
            jpeg = playwright_client.screenshot(release=False)
            if jpeg:
                _browser_last_jpeg = jpeg
                _browser_last_ts = time.time()
                _browser_last_path = "live"
        except Exception as e:
            print(f"[browser-capture] {e}")
        time.sleep(_browser_target_interval)


threading.Thread(target=_browser_capture_loop, daemon=True, name="browser-capture").start()


@app.route("/api/browser/stream")
def api_browser_stream():
    """Server-Sent Events stream of JPEG frames the agent has captured."""
    def generate():
        global _browser_clients
        with _browser_clients_lock:
            _browser_clients += 1
        try:
            last_sent_ts = 0.0
            yield "retry: 5000\n\n"
            while True:
                if _browser_last_jpeg and _browser_last_ts > last_sent_ts:
                    payload = json.dumps({
                        "ts":    _browser_last_ts,
                        "jpeg":  _browser_last_jpeg,
                        "src":   _browser_last_path,
                    })
                    yield f"data: {payload}\n\n"
                    last_sent_ts = _browser_last_ts
                else:
                    yield ": ping\n\n"
                time.sleep(0.5)
        finally:
            with _browser_clients_lock:
                _browser_clients -= 1

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.route("/api/browser/status")
def api_browser_status():
    return jsonify({
        "watching":       PLAYWRIGHT_SCREENSHOT_DIR,
        "dir_exists":     os.path.isdir(PLAYWRIGHT_SCREENSHOT_DIR),
        "clients":        _browser_clients,
        "last_frame_age_s": (time.time() - _browser_last_ts) if _browser_last_ts else None,
        "last_src":       _browser_last_path,
    })


# ── API: Contacts ─────────────────────────────────────────────────────────────
@app.route("/api/contacts")
def api_contacts():
    try:
        data = _watchdog_status()
        contacts = data.get("contacts", []) if data else []
        # Filter out the owner — guests only
        contacts = [c for c in contacts if str(c.get("chat_id", "")) != str(OWNER_ID)]
        return jsonify(contacts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: Guest usage (per-tester daily caps) ──────────────────────────────────
_GUEST_USAGE_DB = r"C:\Users\azfar\metamcp-local\watchdog\guest_usage.db"
_DEFAULT_GUEST_CAP = 50


def _read_guest_usage_db():
    """Read guest_usage.db (SQLite, populated by watchdog/guest_caps.py)."""
    today = time.strftime("%Y-%m-%d")
    rows = []
    try:
        conn = sqlite3.connect(f"file:{_GUEST_USAGE_DB}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        # Outer join so caps/throttle state still come through with zero usage today
        cur = conn.execute("""
            SELECT
              COALESCE(u.chat_id, c.chat_id, s.chat_id) AS chat_id,
              COALESCE(u.messages, 0)                   AS messages,
              COALESCE(c.max_messages, ?)               AS max_messages,
              COALESCE(s.throttled, 0)                  AS throttled
            FROM (SELECT ? AS day_local) d
            LEFT JOIN usage u ON u.day_local = d.day_local
            LEFT JOIN caps  c ON c.chat_id   = u.chat_id
            LEFT JOIN state s ON s.chat_id   = u.chat_id
            UNION
            SELECT chat_id, 0, max_messages, 0 FROM caps
              WHERE chat_id NOT IN (SELECT chat_id FROM usage WHERE day_local = ?)
        """, (_DEFAULT_GUEST_CAP, today, today))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception:
        return []
    return rows


@app.route("/api/guests/usage")
def api_guests_usage():
    """Return per-guest usage rows merged with contact metadata."""
    usage_rows = {r["chat_id"]: r for r in _read_guest_usage_db() if r.get("chat_id")}
    # Merge with contact registry so we can show names
    contacts = []
    try:
        contacts = (_watchdog_status() or {}).get("contacts", [])
    except Exception:
        pass
    contact_map = {str(c.get("chat_id", "")): c for c in contacts if c.get("chat_id")}

    out = []
    seen = set()
    owner = str(OWNER_ID)
    for chat_id, row in usage_rows.items():
        if chat_id == owner:
            continue
        info = contact_map.get(chat_id, {})
        out.append({
            "chat_id":      chat_id,
            "first_name":   info.get("first_name", ""),
            "username":     info.get("username", ""),
            "messages":     int(row.get("messages", 0)),
            "max_messages": int(row.get("max_messages", _DEFAULT_GUEST_CAP)),
            "throttled":    bool(row.get("throttled", 0)),
        })
        seen.add(chat_id)
    # Include contacts who haven't been active today (so they show with 0)
    for chat_id, info in contact_map.items():
        if chat_id == owner or chat_id in seen:
            continue
        out.append({
            "chat_id":      chat_id,
            "first_name":   info.get("first_name", ""),
            "username":     info.get("username", ""),
            "messages":     0,
            "max_messages": _DEFAULT_GUEST_CAP,
            "throttled":    False,
        })
    out.sort(key=lambda r: (-r["messages"], r["first_name"] or r["chat_id"]))
    return jsonify(out)


@app.route("/api/guests/cap", methods=["POST"])
def api_guests_cap_set():
    """Set per-guest cap. Writes directly to guest_usage.db."""
    data = request.json or {}
    chat_id = str(data.get("chat_id", "")).strip()
    cap     = int(data.get("max_messages", 0))
    if not chat_id or cap < 1:
        return jsonify({"error": "chat_id and max_messages>=1 required"}), 400
    try:
        conn = sqlite3.connect(_GUEST_USAGE_DB, timeout=2)
        conn.execute(
            "INSERT INTO caps (chat_id, max_messages) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET max_messages = excluded.max_messages",
            (chat_id, cap),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: Pending Pairings ─────────────────────────────────────────────────────
_OPENCLAW_PAIRING  = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\credentials\telegram-pairing.json"
_OPENCLAW_ALLOWFROM = r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\credentials\telegram-default-allowFrom.json"


def _read_pairing_requests() -> list:
    try:
        with open(_OPENCLAW_PAIRING, encoding="utf-8") as f:
            return (json.load(f) or {}).get("requests", [])
    except Exception:
        return []


def _read_allow_from() -> set[str]:
    try:
        with open(_OPENCLAW_ALLOWFROM, encoding="utf-8") as f:
            return {str(x) for x in (json.load(f) or {}).get("allowFrom", [])}
    except Exception:
        return set()


@app.route("/api/pairing/pending")
def api_pairing_pending():
    """List pairing requests that haven't been approved yet."""
    try:
        approved = _read_allow_from()
        pending = []
        for r in _read_pairing_requests():
            chat_id = str(r.get("id", ""))
            if not chat_id or chat_id in approved:
                continue
            meta = r.get("meta", {}) or {}
            pending.append({
                "chat_id":     chat_id,
                "code":        r.get("code", ""),
                "first_name":  meta.get("firstName", "") or "",
                "username":    meta.get("username", "") or "",
                "created_at":  r.get("createdAt", "") or "",
                "last_seen":   r.get("lastSeenAt", "") or "",
            })
        return jsonify(pending)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pairing/approve", methods=["POST"])
def api_pairing_approve():
    """Approve a pending pairing by running `openclaw pairing approve telegram <code>` in WSL."""
    data = request.json or {}
    code = str(data.get("code", "")).strip()
    if not code or not code.replace("-", "").isalnum():
        return jsonify({"error": "invalid code"}), 400
    try:
        result = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "-u", "azfar", "--",
             "/home/azfar/.npm-global/bin/openclaw", "pairing", "approve", "telegram", code],
            capture_output=True, text=True, timeout=30,
        )
        ok = result.returncode == 0
        return jsonify({
            "ok":     ok,
            "stdout": (result.stdout or "")[-400:],
            "stderr": (result.stderr or "")[-400:],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: Dashboard link ───────────────────────────────────────────────────────
@app.route("/api/send-dashboard", methods=["POST"])
def api_send_dashboard():
    data    = request.json or {}
    chat_id = str(data.get("chat_id", CHAT_IDS["dm"]))
    payload = {"chat_id": chat_id, "text": "⚡ Sentinel Dashboard",
               "reply_markup": {"inline_keyboard": [[
                   {"text": "Open Dashboard", "web_app": {"url": MINI_APP_URL}}]]}}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        return jsonify({"ok": True}) if result.get("ok") else (
            jsonify({"error": result.get("description", "Telegram error")}), 500)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Static ────────────────────────────────────────────────────────────────────
_INDEX_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")

@app.route("/")
def index():
    with open(_INDEX_PATH, encoding="utf-8") as f:
        html = f.read()
    injection = (f'<script>'
                 f'window.SENTINEL_TOKEN="{MINI_APP_SECRET}";'
                 f'window.BOT_USERNAME="{BOT_USERNAME}";'
                 f'</script>')
    html = html.replace("</head>", f"{injection}\n</head>", 1)
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    _write_totp_setup_page()
    def _purge_loop():
        while True:
            time.sleep(3600)
            _purge_expired()
    threading.Thread(target=_purge_loop, daemon=True).start()
    print("Sentinel Mini App v2 Bridge on :8098")
    app.run(host="127.0.0.1", port=8098, debug=False)
