"""
Sentinel Mini App v2 Bridge — port 8098
Auth: Telegram identity → TOTP → session token
"""

import glob
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import threading
import time

# Windows-only: suppress brief cmd windows when subprocess.run/Popen is invoked
# without DETACHED_PROCESS. All subprocess calls below should include _NO_WINDOW.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import keyring
import pyotp
import qrcode

from flask import Flask, jsonify, request, send_from_directory, Response

app = Flask(__name__, static_folder="static")


# ── Environment from .env.local ─────────────────────────────────────────────
# The bot reaches Postgres (BrainStore / eventbus) using POSTGRES_* from the
# environment. When launched by START_AI_STACK.bat those are exported from
# .env.local, but a bare task launcher (SentinelBridge.bat = `python bridge.py`)
# starts the process WITHOUT them — leaving store.dsn passwordless, so every
# BrainStore + listen_events connect fails "fe_sendauth: no password supplied"
# (regression seen 2026-06-12 after a task-based restart). Self-load .env.local
# here so the bridge is robust to ANY launcher. Real env always wins (we only
# fill keys that aren't already set), and parsing is done in Python so secrets
# with '=' / quotes / special chars don't get mangled the way a .bat loop would.
def _load_env_local() -> None:
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local")
    try:
        with open(env_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


_load_env_local()

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


# ── V6 prep: central paths module ─────────────────────────────────────────────
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
# Also put the bridge's own dir on the path so sibling modules (tg_user_mirror,
# brain_routes) import regardless of the process launch directory.
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from _paths import (  # noqa: E402
    REPO_ROOT, SCRIPTS_DIR as _SCRIPTS_DIR_PATH, OPENCLAW_JSON as _OPENCLAW_JSON_PATH,
    MODELS_JSON as _MODELS_JSON_PATH, SESSIONS_DIR as _SESSIONS_DIR_PATH,
    AUTH_PROFILES_JSON as _AUTH_PROFILES_PATH, TELEGRAM_PAIRING as _TG_PAIRING,
    TELEGRAM_ALLOWFROM as _TG_ALLOWFROM, GUEST_USAGE_DB as _GUEST_DB,
    COOKIES_DIR as _DEFAULT_COOKIES_DIR,
)

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION_FILE      = str(REPO_ROOT / "VERSION")
OPENCLAW_JSON     = str(_OPENCLAW_JSON_PATH)
MODELS_JSON       = str(_MODELS_JSON_PATH)
SESSIONS_DIR      = str(_SESSIONS_DIR_PATH)
SHORTCUTS_JSON    = os.path.join(os.path.dirname(__file__), "shortcuts.json")
INFER_BRIDGE      = "http://127.0.0.1:8095/infer_status"
# Phase D rewire (2026-05-25): Mini App now reads from watchdog v2 on :8200
# via v1_compat endpoints (/status, /restart, /api/services). Set
# SENTINEL_WATCHDOG_URL=http://127.0.0.1:8099 to fall back to v1 during E.1
# rollback windows.
WATCHDOG_URL      = os.environ.get("SENTINEL_WATCHDOG_URL", "http://127.0.0.1:8200")
try:
    import keyring as _kr
    WATCHDOG_V2_TOKEN = _kr.get_password("sentinel-watchdog", "v2-service-token") or ""
except Exception:
    WATCHDOG_V2_TOKEN = ""
def _watchdog_headers(extra: dict | None = None) -> dict:
    h = {"X-Sentinel-Service-Token": WATCHDOG_V2_TOKEN} if WATCHDOG_V2_TOKEN else {}
    if extra:
        h.update(extra)
    return h
MEMORY_MCP_URL    = "http://127.0.0.1:8092/mcp"
REMINDERS_MCP_URL = "http://127.0.0.1:8087/mcp"
CONTEXT_TOKENS    = 131072
SCRIPTS_DIR       = str(_SCRIPTS_DIR_PATH)

CHAT_IDS        = _cfg.get("telegram_chat_ids") or {}
if not CHAT_IDS.get("dm"):
    raise RuntimeError(
        "config.json must define telegram_chat_ids.dm (your Telegram user ID). "
        "See QUICK_START.md for setup."
    )
TELEGRAM_TOKEN  = _secret("telegram_bot_token", "TELEGRAM_BOT_TOKEN", "telegram_bot_token")
# Out-of-band Claude→owner ping bot (@Sentinel_claude_testbot_bot). Outbound only.
TESTBOT_TOKEN   = _secret("testbot_token", "TESTBOT_TOKEN", "testbot_token")
# Shared secret gating the local /api/notify ingress (Claude sessions / notify.py).
NOTIFY_TOKEN    = _secret("notify_token", "NOTIFY_TOKEN", "notify_token")
MINI_APP_URL    = _cfg.get("mini_app_url") or "https://your-domain.example.com"
MINI_APP_SECRET = _secret("mini_app_secret",    "MINI_APP_SECRET",    "mini_app_secret")
TOTP_SECRET     = _secret("totp_secret",        "TOTP_SECRET",        "totp_secret")
OWNER_ID        = int(CHAT_IDS["dm"])
BOT_USERNAME    = "YourSentinelBot"

# ── Web Push (VAPID) — native OS push for the installed Suite PWA/TWA ─────────
# Keypair lives in vapid_keys.json (gitignored — the private key is a secret).
# Generated on first run if absent. The public key is handed to the browser as
# the applicationServerKey; the private PEM signs the push request to FCM/APNs.
_VAPID_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vapid_keys.json")


def _load_or_create_vapid() -> dict:
    try:
        with open(_VAPID_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    try:
        import base64 as _b64
        from py_vapid import Vapid01
        from cryptography.hazmat.primitives import serialization
        v = Vapid01()
        v.generate_keys()
        priv_pem = v.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        pub_raw = v.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        data = {"private_pem": priv_pem,
                "public_key_b64": _b64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode(),
                "subject": "mailto:your@email.com"}
        with open(_VAPID_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data
    except Exception as e:
        print(f"[vapid] keys unavailable, web push disabled: {e}")
        return {}


_VAPID        = _load_or_create_vapid()
VAPID_PRIV    = _VAPID.get("private_pem", "")
VAPID_PUB_B64 = _VAPID.get("public_key_b64", "")
VAPID_SUBJECT = _VAPID.get("subject", "mailto:owner@your-domain.example.com")

# ── Restart maps ──────────────────────────────────────────────────────────────
_DOCKER_NAMES: dict[str, str] = {
    "MetaMCP":          "metamcp",
    "Reminders MCP":    "reminders-mcp",
    "SMDL MCP":         "ytdlp-mcp",
    "Google WS MCP":    "google-workspace-mcp",
    "Maps MCP":         "maps-mcp",
    "Memory MCP":       "memory-mcp",
    "GitHub MCP":       "github-mcp",
    "OneDrive MCP":     "onedrive-mcp",
    "Translate MCP":    "translate-mcp",
    "SMDL (s.)":        "smdl",
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
    "vaultwarden":           8085,
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
    {"name": "Vaultwarden",          "port": 8085},
    {"name": "SMDL MCP",             "port": 8088},
    {"name": "SMDL (s.)",            "port": 8096},
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL DEFAULT '',
                body       TEXT NOT NULL DEFAULT '',
                level      TEXT NOT NULL DEFAULT 'info',
                source     TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                read_at    REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint   TEXT PRIMARY KEY,
                p256dh     TEXT NOT NULL,
                auth       TEXT NOT NULL,
                ua         TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        # Owner notification preferences — single row (id=1). Each column is a
        # 0/1 toggle the owner flips in the Suite settings panel. idle_ping is
        # the master switch for the end-of-turn Stop-hook ping; ch_* gate which
        # delivery channels any notification fans out to.
        c.execute("""
            CREATE TABLE IF NOT EXISTS notif_prefs (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                idle_ping   INTEGER NOT NULL DEFAULT 1,
                ch_app      INTEGER NOT NULL DEFAULT 1,
                ch_push     INTEGER NOT NULL DEFAULT 1,
                ch_telegram INTEGER NOT NULL DEFAULT 1
            )
        """)
        c.execute("INSERT OR IGNORE INTO notif_prefs (id) VALUES (1)")
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


# ── Notifications store ───────────────────────────────────────────────────────
_NOTIF_COLS = ("id", "title", "body", "level", "source", "created_at", "read_at")


def _notif_add(title: str, body: str, level: str = "info", source: str = "claude") -> int:
    now = time.time()
    with _sess_wlock:
        cur = _sess_conn().execute(
            "INSERT INTO notifications (title, body, level, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, body, level, source, now),
        )
        _sess_conn().commit()
        return cur.lastrowid


def _notif_list(limit: int = 50) -> list[dict]:
    rows = _sess_conn().execute(
        "SELECT id, title, body, level, source, created_at, read_at "
        "FROM notifications ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(zip(_NOTIF_COLS, r)) for r in rows]


def _notif_unread() -> int:
    return _sess_conn().execute(
        "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"
    ).fetchone()[0]


def _notif_mark_read(nid: int | None = None) -> None:
    now = time.time()
    with _sess_wlock:
        if nid is None:
            _sess_conn().execute(
                "UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (now,))
        else:
            _sess_conn().execute(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                (now, nid))
        _sess_conn().commit()


# ── Notification preferences ──────────────────────────────────────────────────
_PREF_KEYS = ("idle_ping", "ch_app", "ch_push", "ch_telegram")


def _prefs_get() -> dict:
    row = _sess_conn().execute(
        "SELECT idle_ping, ch_app, ch_push, ch_telegram "
        "FROM notif_prefs WHERE id = 1").fetchone()
    if not row:
        return {k: True for k in _PREF_KEYS}
    return {k: bool(v) for k, v in zip(_PREF_KEYS, row)}


def _prefs_set(updates: dict) -> dict:
    cols = {k: (1 if updates[k] else 0) for k in _PREF_KEYS if k in updates}
    if cols:
        assignments = ", ".join(f"{k} = ?" for k in cols)
        with _sess_wlock:
            _sess_conn().execute(
                f"UPDATE notif_prefs SET {assignments} WHERE id = 1",
                tuple(cols.values()))
            _sess_conn().commit()
    return _prefs_get()


# ── Web Push subscriptions ────────────────────────────────────────────────────
def _push_add(endpoint: str, p256dh: str, auth: str, ua: str = "") -> None:
    with _sess_wlock:
        _sess_conn().execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, ua, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, "
            "auth=excluded.auth, ua=excluded.ua",
            (endpoint, p256dh, auth, ua, time.time()))
        _sess_conn().commit()


def _push_remove(endpoint: str) -> None:
    with _sess_wlock:
        _sess_conn().execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        _sess_conn().commit()


def _push_all() -> list[dict]:
    rows = _sess_conn().execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    return [{"endpoint": r[0], "p256dh": r[1], "auth": r[2]} for r in rows]


_VAPID_OBJ = None  # cached py_vapid Vapid01 instance (built from the PEM once)


def _push_send_all(title: str, body: str, level: str = "info", nid: int | None = None) -> dict:
    """Fan a web-push out to every stored subscription. Best-effort; prunes
    subscriptions the push service reports as gone (404/410). Returns a small
    {sent, pruned, failed} tally for the /api/notify response."""
    global _VAPID_OBJ
    if not VAPID_PRIV:
        return {"sent": 0, "pruned": 0, "failed": 0, "disabled": True}
    try:
        from pywebpush import webpush, WebPushException
        from py_vapid import Vapid01
    except Exception:
        return {"sent": 0, "pruned": 0, "failed": 0, "disabled": True}
    # pywebpush can't take a PEM string for vapid_private_key (it base64-decodes
    # it as raw DER → ASN.1 error). It needs a Vapid01 instance built from the PEM.
    if _VAPID_OBJ is None:
        try:
            _VAPID_OBJ = Vapid01.from_pem(VAPID_PRIV.encode())
        except Exception as e:
            print(f"[vapid] PEM load failed, web push disabled: {e}")
            return {"sent": 0, "pruned": 0, "failed": 0, "disabled": True}
    payload = json.dumps({"title": title or "Sentinel", "body": body,
                          "level": level, "id": nid})
    sent = pruned = failed = 0
    for sub in _push_all():
        info = {"endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            webpush(info, payload, vapid_private_key=_VAPID_OBJ,
                    vapid_claims={"sub": VAPID_SUBJECT}, ttl=86400)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                _push_remove(sub["endpoint"])
                pruned += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"sent": sent, "pruned": pruned, "failed": failed}


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


# ── TOTP setup page (local only, opt-in) ─────────────────────────────────────
# Default: headless. The page + the secret are NOT written/printed on every
# restart — that was leaking a long-lived secret into stdout/journald and
# leaving a plaintext-secret HTML file on disk. Set SENTINEL_WRITE_TOTP_SETUP=1
# in the environment when you actually need to re-enrol an authenticator app.
def _write_totp_setup_page():
    if not TOTP_SECRET:
        return
    if os.environ.get("SENTINEL_WRITE_TOTP_SETUP", "") != "1":
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
    print(f"[sentinel-v2] TOTP setup page -> {out} (delete this file after enrolling)")


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


# ── Telethon user-account client (V3 Phase 1.5) ──────────────────────────────
# Used to send messages to the AI bot AS IF the owner typed them in Telegram.
# Reuses the same user-account session ClaudeAssistant uses.
_telethon_client = None
_telethon_loop   = None
_telethon_lock   = threading.Lock()


def _start_telethon_client():
    """Initialise Telethon client in a background asyncio loop. Idempotent."""
    global _telethon_client, _telethon_loop
    api_id_str = _secret("telethon_api_id",   "TELETHON_API_ID",   "telethon_api_id")
    api_hash   = _secret("telethon_api_hash", "TELETHON_API_HASH", "telethon_api_hash")
    session    = _secret("telethon_session",  "TELETHON_SESSION",  "telethon_session")
    if not (api_id_str and api_hash and session):
        print("[telethon] credentials missing in WCM — chat composer disabled")
        return
    try:
        import asyncio
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("[telethon] telethon module not installed — chat composer disabled")
        return

    api_id = int(api_id_str)
    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        client = TelegramClient(StringSession(session), api_id, api_hash, loop=loop)
        loop.run_until_complete(client.connect())
        # Prime the dialogs cache — Telethon stores entities lazily, and the AI
        # bot must be in cache before send_message(int_id) works. Without this
        # the first send fails with "Could not find the input entity for
        # PeerUser(...)" if the user hasn't recently DM'd the bot from this
        # exact session.
        try:
            loop.run_until_complete(client.get_dialogs(limit=200))
        except Exception as _e:
            print(f"[telethon] get_dialogs warning: {_e}")
        global _telethon_client, _telethon_loop
        _telethon_client = client
        _telethon_loop = loop
        print("[telethon] connected (user-account mode, dialogs primed)")
        loop.run_forever()

    threading.Thread(target=_runner, daemon=True, name="telethon-loop").start()


_start_telethon_client()


# ── Cross-surface user-message mirror (Mini App / Tauri → TG as user) ────────
# Uses Telethon (above) to send-as-the-owner so /chat messages appear in your
# TG DM under your name + avatar, not the bot's. Sibling to the assistant-reply
# mirror in openclaw/tg_bot/mirror.py.
try:
    import tg_user_mirror  # type: ignore
    _AI_BOT_ID = int(_cfg.get("ai_bot_user_id", 7552648476))
    tg_user_mirror.start_user_mirror(
        get_telethon=lambda: (_telethon_client, _telethon_loop),
        bot_chat_id=_AI_BOT_ID,
    )
except Exception as _mirror_exc:
    print(f"[tg_user_mirror] start failed: {_mirror_exc}")


# ── LM Studio model autosync (V3.5.x) ────────────────────────────────────────
# Watches LM Studio for downloaded/loaded models and keeps openclaw.json's
# lmstudio provider entries in sync (id, name, contextWindow, contextTokens).
# Runs once at startup + every 5 min. Idempotent — only writes + hot-reloads
# OpenClaw when something actually changed.
def _start_lm_autosync():
    import importlib.util
    sync_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "sync_lm_models.py")
    sync_path = os.path.abspath(sync_path)
    if not os.path.exists(sync_path):
        print(f"[lm-sync] script not found at {sync_path} — autosync disabled")
        return

    spec = importlib.util.spec_from_file_location("sync_lm_models", sync_path)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[lm-sync] failed to import script: {e}")
        return

    def _loop():
        import time as _t
        while True:
            try:
                result = mod.sync(dry_run=False, no_reload=False)
                if result.get("changed"):
                    print(f"[lm-sync] {len(result.get('added', []))} added, "
                          f"{len(result.get('updated', []))} updated, "
                          f"{len(result.get('removed', []))} removed — reloaded={result.get('reloaded', False)}")
            except Exception as e:
                print(f"[lm-sync] error: {e}")
            _t.sleep(300)  # 5 minutes

    threading.Thread(target=_loop, daemon=True, name="lm-autosync").start()


_start_lm_autosync()


@app.route("/api/lmstudio/sync", methods=["POST"])
def api_lmstudio_sync():
    """Manual trigger for LM Studio model autosync. Used by the mini-app
    'Sync models' button in the Settings panel."""
    import importlib.util
    sync_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "sync_lm_models.py")
    sync_path = os.path.abspath(sync_path)
    if not os.path.exists(sync_path):
        return jsonify({"error": "sync script not found"}), 500
    try:
        spec = importlib.util.spec_from_file_location("sync_lm_models", sync_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return jsonify(mod.sync(dry_run=False, no_reload=False))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent/message", methods=["POST"])
def api_agent_message():
    """Send a message to the AI bot as if the owner typed it in Telegram chat.
    Goes through Telethon (user-account API), so OpenClaw processes it as a
    normal user message — full agent response, tools, the works. Used by the
    chat composer in the V3 Browser panel."""
    data = request.json or {}
    text = (data.get("text", "") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if len(text) > 2000:
        return jsonify({"error": "text too long (max 2000 chars)"}), 400
    if not _telethon_client or not _telethon_loop:
        return jsonify({"error": "Telethon not connected — check WCM credentials"}), 503

    # Prefer @username — Telethon resolves it via API even if not in cache.
    # Falls back to numeric ID if username send fails for some reason.
    AI_BOT_USERNAME = _cfg.get("ai_bot_username", BOT_USERNAME)
    AI_BOT_ID       = int(_cfg.get("ai_bot_user_id", 7552648476))
    try:
        import asyncio
        async def _send():
            try:
                return await _telethon_client.send_message(AI_BOT_USERNAME, text)
            except Exception:
                # Fallback path: re-fetch dialogs then retry by ID
                await _telethon_client.get_dialogs(limit=200)
                return await _telethon_client.send_message(AI_BOT_ID, text)
        future = asyncio.run_coroutine_threadsafe(_send(), _telethon_loop)
        msg = future.result(timeout=15)
        return jsonify({"ok": True, "message_id": getattr(msg, "id", None)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Playwright MCP client (V3 browser panel) ──────────────────────────────────
from playwright_client import PlaywrightMCPClient
_metamcp_token = _secret("metamcp_bearer_token", "METAMCP_BEARER_TOKEN", "metamcp_bearer_token")
playwright_client = PlaywrightMCPClient(token=_metamcp_token) if _metamcp_token else None


# ── Telegram helper ───────────────────────────────────────────────────────────
def _tg_post_with(token: str, method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _tg_post(method: str, payload: dict) -> dict:
    return _tg_post_with(TELEGRAM_TOKEN, method, payload)


def _tg_send_document(token: str, chat_id, file_bytes: bytes, filename: str,
                      caption: str = "") -> dict:
    """sendDocument via multipart/form-data (urllib — no `requests` dependency).
    Telegram caps bot uploads at 50 MB; the caller validates size."""
    boundary = "----SentinelDoc" + os.urandom(16).hex()
    crlf = b"\r\n"

    def _field(name: str, value: str) -> bytes:
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f'\r\n\r\n{value}\r\n').encode()

    body = _field("chat_id", str(chat_id))
    if caption:
        body += _field("caption", caption)
        body += _field("parse_mode", "HTML")
    safe_name = (filename or "document").replace('"', "")
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
             f'filename="{safe_name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    body += file_bytes + crlf
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
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
                           timeout=5, capture_output=True, creationflags=_NO_WINDOW)
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


# ── WebAuthn / passkey manager (Phase 4) ──────────────────────────────────────
# Lives in the sibling webauthn_auth module; owns its own table in sessions.db.
# Degrades to None (routes return 503) if the optional `webauthn` lib is absent,
# so a missing dependency never blocks the core Telegram+TOTP login path.
try:
    from webauthn_auth import WebAuthnManager
    _webauthn = WebAuthnManager(db_path=_SESSION_DB, owner_id=OWNER_ID)
except Exception as _wae:  # pragma: no cover
    _webauthn = None
    print(f"[webauthn] passkey support disabled: {_wae}")


# ── Auth middleware ───────────────────────────────────────────────────────────
# Pre-TOTP routes — only need X-Sentinel-Token (embedded in page). The passkey
# LOGIN ceremony is pre-session (it mints a session), so its options/verify and
# the availability probe live here. Passkey REGISTRATION + management stay under
# the full session gate (you can only add a passkey while already logged in).
_PRE_TOTP  = {"/api/auth/telegram", "/api/auth/verify", "/api/auth/status",
              "/api/auth/webauthn/login/options", "/api/auth/webauthn/login/verify",
              "/api/auth/webauthn/available"}
# Exempt — called locally by OpenClaw without any token
# /api/auth/device gates itself with owner_token, so it's exempt from the
# session-token middleware (its whole purpose is to MINT a session).
_EXEMPT    = {"/api/send-dashboard", "/api/auth/device", "/api/notify",
              "/api/notify-document",
              "/api/notifications", "/api/notifications/read-all",
              "/api/notify/prefs",
              "/api/push/vapid-public", "/api/push/subscribe", "/api/push/unsubscribe"}
# /api/notifications/<id>/read is variable-path; matched via prefix in check_auth.

@app.before_request
def check_auth():
    if not request.path.startswith("/api/"):
        return
    if request.method == "OPTIONS":
        return
    if request.path in _EXEMPT:
        return
    # Notification read/mark endpoints self-gate (owner cookie / session / local).
    if request.path.startswith("/api/notifications/") and request.path.endswith("/read"):
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

    # Phase E (2026-05-11) — hot-reload TOTP_SECRET from WCM per verify
    # call instead of relying on module-level cache. Rotation script
    # (rotate_totp_secret.ps1) now updates WCM; this read picks it up
    # immediately without bridge restart.
    _totp_current = _secret("totp_secret", "TOTP_SECRET", "totp_secret") or TOTP_SECRET
    if _totp_current:
        if not pyotp.TOTP(_totp_current).verify(code, valid_window=1):
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


@app.route("/api/auth/device", methods=["POST"])
def api_auth_device():
    """Headless-client bootstrap (Phase 6 — Tauri admin chat).

    POST { "owner_token": "<...>" } → { "session_token": "<...>", "expires_at": <epoch> }

    Trades the bridge's `OWNER_AUTH_TOKEN` (same one /auth/setup accepts) for
    a fresh session_token. Rate-limited like the regular auth flow."""
    ip = _client_ip()
    if not _rate_check(ip):
        return jsonify({"error": "too_many_attempts", "retry_after": RATE_WINDOW}), 429
    data = request.json or {}
    token = (data.get("owner_token") or "").strip()
    if not OWNER_AUTH_TOKEN or not _safe_token_eq(token, OWNER_AUTH_TOKEN):
        _rate_fail(ip)
        return jsonify({"error": "invalid_token"}), 401
    _rate_clear(ip)
    ua = (request.headers.get("User-Agent") or "")[:120]
    tok, exp = _new_session(OWNER_ID, ip, ua + " device")
    return jsonify({"session_token": tok, "expires_at": exp})


# ── API: Auth — WebAuthn / passkeys (Phase 4) ─────────────────────────────────
@app.route("/api/auth/webauthn/available")
def api_webauthn_available():
    """Pre-session probe: does the owner have at least one passkey registered?
    Lets the login screen decide whether to offer the passkey button."""
    if _webauthn is None:
        return jsonify({"available": False, "supported": False})
    try:
        return jsonify({"available": _webauthn.has_credentials(), "supported": True})
    except Exception:
        return jsonify({"available": False, "supported": True})


@app.route("/api/auth/webauthn/login/options", methods=["POST"])
def api_webauthn_login_options():
    if _webauthn is None:
        return jsonify({"error": "passkeys_unavailable"}), 503
    ip = _client_ip()
    if not _rate_check(ip):
        return jsonify({"error": "too_many_attempts", "retry_after": RATE_WINDOW}), 429
    if not _webauthn.has_credentials():
        return jsonify({"error": "no_passkeys"}), 404
    opts, handle = _webauthn.authentication_options()
    return jsonify({"options": opts, "handle": handle})


@app.route("/api/auth/webauthn/login/verify", methods=["POST"])
def api_webauthn_login_verify():
    if _webauthn is None:
        return jsonify({"error": "passkeys_unavailable"}), 503
    ip = _client_ip()
    if not _rate_check(ip):
        return jsonify({"error": "too_many_attempts", "retry_after": RATE_WINDOW}), 429
    data = request.json or {}
    handle = data.get("handle", "")
    credential = data.get("credential") or {}
    if not handle or not isinstance(credential, dict):
        _rate_fail(ip)
        return jsonify({"error": "bad_request"}), 400
    if not _webauthn.authentication_verify(handle, credential):
        _rate_fail(ip)
        return jsonify({"error": "assertion_failed"}), 401
    _rate_clear(ip)
    ua = (request.headers.get("User-Agent") or "")[:120]
    tok, exp = _new_session(OWNER_ID, ip, ua + " passkey")
    return jsonify({"ok": True, "session_token": tok, "expires_at": exp})


@app.route("/api/auth/webauthn/register/options", methods=["POST"])
def api_webauthn_register_options():
    # Session-gated by before_request — caller is already the authenticated owner.
    if _webauthn is None:
        return jsonify({"error": "passkeys_unavailable"}), 503
    opts, handle = _webauthn.registration_options()
    return jsonify({"options": opts, "handle": handle})


@app.route("/api/auth/webauthn/register/verify", methods=["POST"])
def api_webauthn_register_verify():
    if _webauthn is None:
        return jsonify({"error": "passkeys_unavailable"}), 503
    data = request.json or {}
    handle = data.get("handle", "")
    credential = data.get("credential") or {}
    label = str(data.get("label", "") or "Passkey")
    if not handle or not isinstance(credential, dict):
        return jsonify({"error": "bad_request"}), 400
    try:
        info = _webauthn.registration_verify(handle, credential, label)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "verify_failed", "detail": str(e)[:200]}), 400
    return jsonify({"ok": True, "credential": info})


@app.route("/api/auth/webauthn/credentials")
def api_webauthn_credentials():
    if _webauthn is None:
        return jsonify({"credentials": [], "supported": False})
    return jsonify({"credentials": _webauthn.list_credentials(), "supported": True})


@app.route("/api/auth/webauthn/credentials/<cred_id>", methods=["DELETE"])
def api_webauthn_credential_delete(cred_id):
    if _webauthn is None:
        return jsonify({"error": "passkeys_unavailable"}), 503
    if not _re.fullmatch(r"[A-Za-z0-9_-]{1,256}", cred_id):
        return jsonify({"error": "bad_credential_id"}), 400
    ok = _webauthn.delete_credential(cred_id)
    return jsonify({"ok": ok}) if ok else (jsonify({"error": "not_found"}), 404)


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


_AUTH_PROFILES_JSON = str(_AUTH_PROFILES_PATH)


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
                       timeout=5, capture_output=True, creationflags=_NO_WINDOW)
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


# ── API: Tool Drawer ──────────────────────────────────────────────────────────
# Inventory + enable/disable MCP tools per namespace. Backed by MetaMCP's
# namespace_tool_mappings table (status enum: ACTIVE | INACTIVE).
#
# Default behaviour: tools whose server is mapped to the namespace are
# implicitly ACTIVE even with no row in namespace_tool_mappings. We INSERT
# rows lazily — only when the user explicitly toggles. Disable = upsert
# status=INACTIVE; enable = upsert status=ACTIVE (could also delete, but
# upserting both keeps the table the source of truth).
#
# Queries hit metamcp-pg via `docker exec` so credentials never touch this
# process. ~150 ms per call — fine for an admin UI.

import re as _re
import subprocess as _subprocess

_DEFAULT_NAMESPACE_UUID = "0a83b85b-24ea-4491-b24b-17104bc9bba0"
_UUID_RE = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _pg_query(sql: str) -> list[dict]:
    """Run a SELECT, return rows as list[dict] via jsonb_agg.

    SQL must reference no variables that come from untrusted input — we
    parameterise nowhere. Callers MUST sanitise inputs (UUID regex, enum
    allowlist) before string-formatting them in."""
    wrapped = f"SELECT COALESCE(jsonb_agg(t), '[]'::jsonb) FROM ({sql}) AS t;"
    r = _subprocess.run(
        ["docker", "exec", "-i", "metamcp-pg",
         "psql", "-U", "metamcp_user", "-d", "metamcp_db",
         "-At", "-c", wrapped],
        capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout.strip() or "[]")


def _pg_execute(sql: str) -> None:
    """Run an UPDATE/INSERT/DELETE. Same sanitisation responsibility as _pg_query."""
    r = _subprocess.run(
        ["docker", "exec", "-i", "metamcp-pg",
         "psql", "-U", "metamcp_user", "-d", "metamcp_db", "-c", sql],
        capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()[:200]}")


@app.route("/api/tools/servers")
def api_tools_servers():
    """List MCP servers mapped to the Default namespace, with tool counts +
    per-server enabled/disabled headline."""
    ns = _DEFAULT_NAMESPACE_UUID
    rows = _pg_query(f"""
        SELECT
          s.name,
          s.type,
          s.url,
          s.error_status,
          (SELECT COUNT(*) FROM tools t WHERE t.mcp_server_uuid = s.uuid) AS tools_total,
          (SELECT COUNT(*) FROM namespace_tool_mappings ntm
             JOIN tools t ON ntm.tool_uuid = t.uuid
             WHERE ntm.namespace_uuid = '{ns}'
               AND t.mcp_server_uuid = s.uuid
               AND ntm.status = 'INACTIVE') AS tools_disabled
        FROM mcp_servers s
        JOIN namespace_server_mappings nsm
          ON nsm.mcp_server_uuid = s.uuid
        WHERE nsm.namespace_uuid = '{ns}'
        ORDER BY LOWER(s.name)
    """)
    return jsonify({"servers": rows})


@app.route("/api/tools/server/<name>/tools")
def api_tools_for_server(name: str):
    """List tools for one MCP server with current enabled state in this namespace."""
    if not _re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", name):
        return jsonify({"error": "bad server name"}), 400
    ns = _DEFAULT_NAMESPACE_UUID
    name_sql = name.replace("'", "''")
    rows = _pg_query(f"""
        SELECT
          t.uuid::text   AS tool_uuid,
          t.name         AS tool_name,
          t.description  AS description,
          COALESCE(ntm.status::text, 'ACTIVE') AS status,
          (ntm.uuid IS NOT NULL) AS explicitly_mapped
        FROM tools t
        JOIN mcp_servers s ON s.uuid = t.mcp_server_uuid
        LEFT JOIN namespace_tool_mappings ntm
          ON ntm.tool_uuid = t.uuid
         AND ntm.namespace_uuid = '{ns}'
        WHERE s.name = '{name_sql}'
        ORDER BY LOWER(t.name)
    """)
    return jsonify({"server": name, "tools": rows})


@app.route("/api/tools/toggle", methods=["POST"])
def api_tools_toggle():
    """Body: {tool_uuid: <uuid>, enabled: bool}. UPSERTs the mapping row."""
    data = request.json or {}
    tool_uuid = (data.get("tool_uuid") or "").strip().lower()
    enabled   = bool(data.get("enabled"))
    if not _UUID_RE.match(tool_uuid):
        return jsonify({"error": "bad tool_uuid"}), 400

    status = "ACTIVE" if enabled else "INACTIVE"
    ns = _DEFAULT_NAMESPACE_UUID

    # We need the server_uuid for the new row. Look it up from the tool.
    server_rows = _pg_query(f"""
        SELECT mcp_server_uuid::text AS sid FROM tools
        WHERE uuid = '{tool_uuid}'
    """)
    if not server_rows:
        return jsonify({"error": "tool not found"}), 404
    server_uuid = server_rows[0]["sid"]

    _pg_execute(f"""
        INSERT INTO namespace_tool_mappings
            (namespace_uuid, tool_uuid, mcp_server_uuid, status)
        VALUES ('{ns}', '{tool_uuid}', '{server_uuid}', '{status}')
        ON CONFLICT (namespace_uuid, tool_uuid)
        DO UPDATE SET status = EXCLUDED.status;
    """)
    return jsonify({"ok": True, "tool_uuid": tool_uuid, "status": status})


# ── API: Inference ────────────────────────────────────────────────────────────
@app.route("/api/inference/status")
def api_inference_status():
    force = request.args.get("force") == "1"
    return jsonify(get_inference_status(force=force))

@app.route("/api/inference/block", methods=["POST"])
def api_inference_block():
    data = request.json or {}
    block = bool(data.get("blocked"))
    path = "/infer_block" if block else "/infer_unblock"
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:8095{path}", data=b"", method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/inference/restart", methods=["POST"])
def api_inference_restart():
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        for line in result.stdout.splitlines():
            if ":8095 " in line and "LISTENING" in line:
                pid = line.split()[-1]
                subprocess.run(["taskkill", "/PID", pid, "/F"], timeout=3, capture_output=True, creationflags=_NO_WINDOW)
                break
        time.sleep(1)
        subprocess.Popen(["py", "-3", str(REPO_ROOT / "infer_bridge.py")],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: Services ─────────────────────────────────────────────────────────────
def _watchdog_status() -> dict | None:
    """Fetch full status from watchdog. Returns None on failure."""
    try:
        req = urllib.request.Request(f"{WATCHDOG_URL}/status", headers=_watchdog_headers())
        with urllib.request.urlopen(req, timeout=5) as r:
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
        req = urllib.request.Request(f"{WATCHDOG_URL}/versions", headers=_watchdog_headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/services/restart", methods=["POST"])
def api_services_restart():
    """Forward to watchdog's /restart endpoint."""
    data  = request.json or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label required"}), 400
    try:
        req = urllib.request.Request(
            f"{WATCHDOG_URL}/restart",
            data=json.dumps({"label": label}).encode(),
            headers=_watchdog_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return jsonify(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        return jsonify({"error": e.read().decode()[:200]}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/services/logs", methods=["POST"])
def api_services_logs():
    """Forward to watchdog's /logs endpoint."""
    data      = request.json or {}
    container = (data.get("container") or "").strip()
    lines     = int(data.get("lines") or 50)
    if not container:
        return jsonify({"error": "container required"}), 400
    try:
        req = urllib.request.Request(
            f"{WATCHDOG_URL}/logs",
            data=json.dumps({"container": container, "lines": lines}).encode(),
            headers=_watchdog_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return jsonify(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        return jsonify({"error": e.read().decode()[:200]}), e.code
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
            headers=_watchdog_headers({"Content-Type": "application/json"}),
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
                               timeout=30, capture_output=True, creationflags=_NO_WINDOW)
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
                timeout=35, capture_output=True, creationflags=_NO_WINDOW,
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
                       timeout=5, capture_output=True, creationflags=_NO_WINDOW)
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
                       timeout=5, capture_output=True, creationflags=_NO_WINDOW)
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


# ── API: Secrets (Keys card) ──────────────────────────────────────────────────
import secrets_backend  # noqa: E402

@app.route("/api/openclaw/secrets")
def api_secrets_list():
    return jsonify(secrets_backend.list_secrets())


@app.route("/api/openclaw/secrets/<name>/rotate", methods=["POST"])
def api_secrets_rotate(name):
    data  = request.json or {}
    value = data.get("value")  # may be None for autogen
    regen = bool(data.get("regen"))
    result = secrets_backend.rotate(name, value, regen=regen)
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@app.route("/api/openclaw/secrets/<name>/test", methods=["POST"])
def api_secrets_test(name):
    return jsonify(secrets_backend.smoke_test_only(name))


# ── API: OpenClaw Doctor ──────────────────────────────────────────────────────
@app.route("/api/openclaw/doctor")
def api_openclaw_doctor():
    checks = []

    try:
        r = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
             "systemctl is-active openclaw-gateway.service"],
            timeout=8, capture_output=True, text=True, creationflags=_NO_WINDOW,
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
            timeout=10, capture_output=True, text=True, creationflags=_NO_WINDOW,
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


# ── API: Browser stream (V3 Phase 1.0 polling + V3.4 CDP screencast) ─────────
# Two paths, picked dynamically:
# 1. CDP screencast (preferred when available) — connects to a Chromium running
#    with --remote-debugging-port. Page.startScreencast pushes frames at native
#    paint rate (much higher fps + lower latency than polling).
# 2. Playwright MCP polling (fallback) — works without our own Chromium; ~2 fps.
# Toggled by whether CDP is reachable. No config needed; bridge auto-detects.
_browser_clients = 0
_browser_clients_lock = threading.Lock()
_browser_target_interval = 0.5  # fallback polling rate
_browser_last_jpeg = None
_browser_last_ts   = 0.0
_browser_last_path = ""
_browser_source    = "polling"  # or "cdp"

from cdp_client import CDPClient, background_screencast_loop
_cdp = CDPClient(host="127.0.0.1", port=9222)


def _browser_on_frame(jpeg_b64: str, page_url: str):
    global _browser_last_jpeg, _browser_last_ts, _browser_last_path, _browser_source
    _browser_last_jpeg = jpeg_b64
    _browser_last_ts   = time.time()
    _browser_last_path = page_url or "live"
    _browser_source    = "cdp"


def _browser_should_capture() -> bool:
    with _browser_clients_lock:
        return _browser_clients > 0


def _browser_polling_loop():
    """Fallback path — Playwright MCP screenshot polling. Only runs when CDP
    is NOT producing frames. Detected by: last frame age > 5 sec and CDP
    client not connected."""
    global _browser_last_jpeg, _browser_last_ts, _browser_last_path, _browser_source
    while True:
        with _browser_clients_lock:
            n = _browser_clients
        # Poll only if no clients connected to skip work
        if n == 0 or not playwright_client:
            time.sleep(1.0)
            continue
        # Skip polling if CDP is actively delivering frames
        if _cdp.connected and (time.time() - _browser_last_ts) < 3.0:
            time.sleep(1.0)
            continue
        try:
            jpeg = playwright_client.screenshot(release=False)
            if jpeg:
                _browser_last_jpeg = jpeg
                _browser_last_ts = time.time()
                _browser_last_path = "polling"
                _browser_source    = "polling"
        except Exception as e:
            print(f"[browser-poll] {e}")
        time.sleep(_browser_target_interval)


# Start CDP screencast loop (auto-reconnects, no-ops if Chromium not running)
threading.Thread(
    target=background_screencast_loop,
    args=(_cdp, _browser_on_frame, _browser_should_capture),
    daemon=True, name="browser-cdp",
).start()
# Polling fallback also runs — only takes screenshots if CDP isn't delivering
threading.Thread(target=_browser_polling_loop, daemon=True, name="browser-poll").start()


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
        "source":         _browser_source,
        "cdp_connected":  _cdp.connected,
        "cdp_target_url": _cdp.target_url,
    })


# ── V3.4 — CDP-backed scroll endpoint (real mouseWheel) ──────────────────────
COOKIES_DIR = _cfg.get("cookies_dir") or str(_DEFAULT_COOKIES_DIR)


def _parse_netscape_cookies(path: str, filter_domain: str = "") -> list[dict]:
    """Parse a Netscape-format cookies.txt (yt-dlp / curl format) into CDP-ready
    cookie dicts. Format: domain<TAB>flag<TAB>path<TAB>secure<TAB>expiration<TAB>name<TAB>value
    Lines starting with # are comments. Empty lines ignored.

    If filter_domain given, only return cookies whose .domain matches (suffix-aware
    so '.shopee.sg' matches request domain 'shopee.sg' or 'www.shopee.sg')."""
    out = []
    fd = filter_domain.lower().lstrip(".") if filter_domain else ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    # Some exporters use "#HttpOnly_..." prefix to mark http-only;
                    # honour that.
                    if line.startswith("#HttpOnly_"):
                        line = line[len("#HttpOnly_"):]
                        http_only = True
                    else:
                        continue
                else:
                    http_only = False
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, ckpath, secure, expires, name, value = parts[:7]
                domain = domain.strip()
                if fd:
                    d_match = domain.lstrip(".").lower()
                    if not (d_match == fd or d_match.endswith("." + fd) or fd.endswith("." + d_match)):
                        continue
                ck = {
                    "name":   name,
                    "value":  value,
                    "domain": domain,
                    "path":   ckpath or "/",
                    "secure": secure.upper() == "TRUE",
                    "httpOnly": http_only,
                }
                try:
                    exp = float(expires)
                    if exp > 0:
                        ck["expires"] = exp
                except ValueError:
                    pass
                out.append(ck)
    except FileNotFoundError:
        pass
    return out


@app.route("/api/browser/import-cookies", methods=["POST"])
def api_browser_import_cookies():
    """Import cookies from the user's pre-exported cookies.txt files (same
    directory yt-dlp / smdl reads from), then inject into the agent browser
    via CDP Network.setCookies.

    Why this pattern instead of live Chrome extraction: Chrome 127+ ships
    App-Bound Encryption which blocks all third-party cookie readers. Manual
    export via a Cookie-Editor / Get-cookies.txt browser extension is the
    only reliable cross-Chromium path. User exports once per site, saves to
    G:\\YT-DLP\\cookies\\<name>.txt, and the agent inherits the session.

    POST body: {"domain": "shopee.sg"} or omit to use current page's domain.

    Security: gated by mini-app session token (full session-takeover power)."""
    if not _cdp.connected:
        return jsonify({"error": "CDP not connected — Chromium with --remote-debugging-port required"}), 503

    data = request.json or {}
    domain = (data.get("domain") or "").strip().lower()
    if not domain:
        url = _cdp.target_url or ""
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
            domain = host[4:] if host.startswith("www.") else host
        except Exception:
            domain = ""
    if not domain:
        return jsonify({"error": "domain required (or navigate to a page first)"}), 400

    # Find candidate cookies.txt files: scan COOKIES_DIR, match cookies whose
    # domain field is a suffix of (or equal to) the requested domain.
    if not os.path.isdir(COOKIES_DIR):
        return jsonify({
            "error": f"cookies dir not found: {COOKIES_DIR}",
            "hint": "Export cookies via the 'Get cookies.txt LOCALLY' or 'Cookie-Editor' Chrome extension and save to this directory.",
        }), 404

    cookies_out: list[dict] = []
    files_scanned = []
    for fname in os.listdir(COOKIES_DIR):
        if not fname.lower().endswith(".txt"):
            continue
        path = os.path.join(COOKIES_DIR, fname)
        files_scanned.append(fname)
        cookies_out.extend(_parse_netscape_cookies(path, filter_domain=domain))

    if not cookies_out:
        return jsonify({
            "ok": True, "imported": 0, "domain": domain,
            "scanned": files_scanned,
            "note": (f"No cookies found for {domain}. Export via 'Get cookies.txt LOCALLY' "
                     f"Chrome extension and save to {COOKIES_DIR}\\<sitename>.txt"),
        })

    sent = _cdp.set_cookies(cookies_out)
    return jsonify({
        "ok": True, "imported": sent, "domain": domain,
        "names": sorted({c["name"] for c in cookies_out})[:20],
        "scanned": files_scanned,
    })


@app.route("/api/browser/scroll", methods=["POST"])
def api_browser_scroll():
    """Scroll via CDP Input.dispatchMouseEvent type=mouseWheel. Falls back to
    a no-op if CDP isn't connected (polling mode has no scroll-replay path)."""
    if not _cdp.connected:
        return jsonify({"error": "CDP not connected — scroll requires Chromium with --remote-debugging-port"}), 503
    data = request.json or {}
    x = int(data.get("x", 640))
    y = int(data.get("y", 400))
    delta_y = int(data.get("deltaY", 100))
    _cdp.scroll(x, y, delta_y)
    return jsonify({"ok": True, "via": "cdp"})


# ── Phase 1.3 — Co-pilot input forwarding ────────────────────────────────────
# Owner clicks/types in the mini app canvas; bridge forwards to Playwright via
# MCP. Page coordinates are page-pixel coordinates (NOT canvas pixels — mini
# app pre-scales them based on the rendered frame's natural dimensions).

@app.route("/api/browser/click", methods=["POST"])
def api_browser_click():
    if not playwright_client:
        return jsonify({"error": "Playwright MCP not configured"}), 503
    data = request.json or {}
    x = int(data.get("x", -1))
    y = int(data.get("y", -1))
    if x < 0 or y < 0:
        return jsonify({"error": "x and y required (page coordinates)"}), 400
    # Use browser_evaluate — Playwright MCP doesn't expose pixel-coord click
    # directly, but document.elementFromPoint(x,y).click() works for ~95% of
    # HTML elements. SVG/canvas-only pages need a different approach (V3.x).
    js = (
        f"(() => {{"
        f"  const e = document.elementFromPoint({x},{y});"
        f"  if (!e) return {{ok:false, reason:'no element at point'}};"
        f"  e.click();"
        f"  return {{ok:true, tag:e.tagName, text:(e.innerText||'').slice(0,80)}};"
        f"}})()"
    )
    try:
        result = playwright_client._call("Playwright__browser_evaluate",
                                          {"function": js})
        # Free the session so the agent can use the browser
        playwright_client._force_release_session()
        for item in result.get("result", {}).get("content", []):
            if item.get("type") == "text":
                return jsonify({"ok": True, "result": item.get("text", "")[:200]})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/browser/type", methods=["POST"])
def api_browser_type():
    if not playwright_client:
        return jsonify({"error": "Playwright MCP not configured"}), 503
    data = request.json or {}
    text = (data.get("text", "") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if len(text) > 500:
        return jsonify({"error": "text too long (max 500 chars)"}), 400
    # Use browser_evaluate to focus the active element and dispatch keys.
    # This is more reliable than browser_type which needs an element ref.
    escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    js = (
        f"(() => {{"
        f"  const el = document.activeElement;"
        f"  if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && !el.isContentEditable)) {{"
        f"    return {{ok:false, reason:'no editable element focused'}};"
        f"  }}"
        f"  if (el.isContentEditable) {{ document.execCommand('insertText', false, '{escaped}'); }}"
        f"  else {{ el.value = (el.value || '') + '{escaped}'; el.dispatchEvent(new Event('input', {{bubbles:true}})); }}"
        f"  return {{ok:true, tag:el.tagName, name:el.name||el.id||''}};"
        f"}})()"
    )
    try:
        result = playwright_client._call("Playwright__browser_evaluate",
                                          {"function": js})
        playwright_client._force_release_session()
        for item in result.get("result", {}).get("content", []):
            if item.get("type") == "text":
                return jsonify({"ok": True, "result": item.get("text", "")[:200]})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/browser/key", methods=["POST"])
def api_browser_key():
    """Send a single key (Enter, Escape, Tab, etc.) — used for form submission."""
    if not playwright_client:
        return jsonify({"error": "Playwright MCP not configured"}), 503
    data = request.json or {}
    key = (data.get("key", "") or "").strip()
    if key not in {"Enter", "Tab", "Escape", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"}:
        return jsonify({"error": "key not allowed"}), 400
    try:
        result = playwright_client._call("Playwright__browser_press_key", {"key": key})
        playwright_client._force_release_session()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
_GUEST_USAGE_DB = str(_GUEST_DB)
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
_OPENCLAW_PAIRING  = str(_TG_PAIRING)
_OPENCLAW_ALLOWFROM = str(_TG_ALLOWFROM)


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
    from _paths import WSL_DISTRO as _WSL_D, WSL_USER as _WSL_U, OPENCLAW_NPM_BIN_BASH as _NPM_BIN
    try:
        result = subprocess.run(
            ["wsl", "-d", _WSL_D, "-u", _WSL_U, "--",
             _NPM_BIN, "pairing", "approve", "telegram", code],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
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


# ── API: Notifications ────────────────────────────────────────────────────────
# Inbound ping ingress for Claude sessions (notify.py / direct curl). Gated by
# NOTIFY_TOKEN (header X-Notify-Token); if that secret is unset, only localhost
# callers are accepted. Exempt from the session middleware (see _EXEMPT).
_NOTIF_ICONS = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "🚨"}


@app.route("/api/notify", methods=["POST"])
def api_notify():
    if NOTIFY_TOKEN:
        supplied = request.headers.get("X-Notify-Token", "")
        if not hmac.compare_digest(supplied, NOTIFY_TOKEN):
            return jsonify({"error": "notify_token_invalid"}), 401
    elif _client_ip() not in ("127.0.0.1", "::1"):
        return jsonify({"error": "notify_not_configured"}), 403

    data    = request.json or {}
    title   = str(data.get("title", "")).strip()
    body    = str(data.get("body", "")).strip()
    level   = (str(data.get("level", "info")).strip() or "info").lower()
    channel = (str(data.get("channel", "both")).strip() or "both").lower()
    source  = str(data.get("source", "claude")).strip() or "claude"
    event   = str(data.get("event", "general")).strip().lower() or "general"
    if not title and not body:
        return jsonify({"error": "title_or_body_required"}), 400
    if channel not in ("app", "bot", "both"):
        return jsonify({"error": "invalid_channel"}), 400

    # Honour the owner's saved preferences (Suite settings panel). The master
    # idle_ping switch suppresses the end-of-turn Stop-hook ping entirely; the
    # ch_* toggles gate which channels any notification actually fans out to.
    prefs = _prefs_get()
    if event == "idle" and not prefs["idle_ping"]:
        return jsonify({"ok": True, "suppressed": "idle_disabled", "event": event})
    want_app = channel in ("app", "both") and prefs["ch_app"]
    want_push = channel in ("app", "both") and prefs["ch_push"]
    want_bot = channel in ("bot", "both") and prefs["ch_telegram"]

    out = {"app": None, "bot": None, "push": None}
    nid = None
    if want_app:
        nid = _notif_add(title, body, level, source)
        out["app"] = {"ok": True, "id": nid}
    if want_push:
        out["push"] = _push_send_all(title, body, level, nid)
    if want_bot:
        if not TESTBOT_TOKEN:
            out["bot"] = {"ok": False, "error": "testbot_token_missing"}
        else:
            icon = _NOTIF_ICONS.get(level, "🔔")
            if title and body:
                text = f"{icon} <b>{html.escape(title)}</b>\n{html.escape(body)}"
            else:
                text = f"{icon} {html.escape(title or body)}"
            try:
                tg = _tg_post_with(TESTBOT_TOKEN, "sendMessage", {
                    "chat_id": OWNER_ID, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True})
                out["bot"] = {"ok": bool(tg.get("ok")), "error": tg.get("description")}
            except Exception as e:
                out["bot"] = {"ok": False, "error": str(e)}

    return jsonify({"ok": True, "id": nid, "channel": channel, "result": out})


@app.route("/api/notify-document", methods=["POST"])
def api_notify_document():
    """Send a FILE to the owner on Telegram (sendDocument) via the testbot — the
    document sibling of /api/notify (which is text-only). Same auth gate (NOTIFY_TOKEN
    or localhost) and the same ch_telegram pref. Two input modes: a multipart upload
    (field `document`) OR — for local callers like notify.py — a server-side `path`
    the bridge reads. Caps at Telegram's 50 MB bot limit."""
    if NOTIFY_TOKEN:
        supplied = request.headers.get("X-Notify-Token", "")
        if not hmac.compare_digest(supplied, NOTIFY_TOKEN):
            return jsonify({"error": "notify_token_invalid"}), 401
    elif _client_ip() not in ("127.0.0.1", "::1"):
        return jsonify({"error": "notify_not_configured"}), 403

    _MAX = 50 * 1024 * 1024
    caption = ""
    filename = None
    file_bytes = None
    up = request.files.get("document")
    if up is not None:
        filename = up.filename or "document"
        file_bytes = up.read()
        caption = (request.form.get("caption") or "").strip()
    else:
        data = request.get_json(silent=True) or request.form or {}
        path = str(data.get("path", "")).strip()
        caption = str(data.get("caption", "")).strip()
        if not path:
            return jsonify({"error": "path_or_document_required"}), 400
        if not os.path.isfile(path):
            return jsonify({"error": "file_not_found"}), 400
        if os.path.getsize(path) > _MAX:
            return jsonify({"error": "file_too_large_50mb"}), 400
        with open(path, "rb") as fh:
            file_bytes = fh.read()
        filename = os.path.basename(path)
    if not file_bytes:
        return jsonify({"error": "empty_file"}), 400
    if len(file_bytes) > _MAX:
        return jsonify({"error": "file_too_large_50mb"}), 400

    prefs = _prefs_get()
    if not prefs["ch_telegram"]:
        return jsonify({"ok": True, "suppressed": "telegram_disabled", "file": filename})
    if not TESTBOT_TOKEN:
        return jsonify({"ok": False, "error": "testbot_token_missing"})
    try:
        tg = _tg_send_document(TESTBOT_TOKEN, OWNER_ID, file_bytes, filename,
                               html.escape(caption) if caption else "")
        return jsonify({"ok": bool(tg.get("ok")), "error": tg.get("description"),
                        "file": filename, "bytes": len(file_bytes)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Read/mark gate: the in-app feed is shared across both *.your-domain.example.com hubs.
# The agent dashboard authenticates with its session token; the Suite launcher
# proxies here server-side and forwards the owner's domain-wide sentinel_apk_session
# cookie (signed with OWNER_AUTH_TOKEN), which _verify_apk_cookie validates. Local
# callers (notify.py et al.) always pass.
def _notif_read_authed() -> bool:
    if _client_ip() in ("127.0.0.1", "::1"):
        return True
    if _verify_apk_cookie(request.cookies.get(APK_COOKIE_NAME, "")):
        return True
    session_token = (request.headers.get("X-Session-Token", "")
                     or request.args.get("session", ""))
    return _valid_session(session_token)


@app.route("/api/notifications")
def api_notifications_list():
    if not _notif_read_authed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify({"notifications": _notif_list(), "unread": _notif_unread()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications/read-all", methods=["POST"])
def api_notifications_read_all():
    if not _notif_read_authed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        _notif_mark_read(None)
        return jsonify({"ok": True, "unread": _notif_unread()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications/<int:nid>/read", methods=["POST"])
def api_notifications_read(nid):
    if not _notif_read_authed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        _notif_mark_read(nid)
        return jsonify({"ok": True, "unread": _notif_unread()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Notification preferences (owner-gated; same reach as the feed) ────────────
@app.route("/api/notify/prefs", methods=["GET"])
def api_notify_prefs_get():
    if not _notif_read_authed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify({"prefs": _prefs_get()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notify/prefs", methods=["POST"])
def api_notify_prefs_set():
    if not _notif_read_authed():
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or {}
    updates = {k: bool(data[k]) for k in _PREF_KEYS if k in data}
    if not updates:
        return jsonify({"error": "no_valid_keys", "allowed": list(_PREF_KEYS)}), 400
    try:
        return jsonify({"ok": True, "prefs": _prefs_set(updates)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Web Push: subscribe / unsubscribe / public key ───────────────────────────
# Same shared-feed reach as the read endpoints: subscribe/unsubscribe are
# owner-gated (_notif_read_authed). The VAPID public key is not a secret, so
# /vapid-public is open (the owner-only page is what fetches it anyway).
@app.route("/api/push/vapid-public")
def api_push_vapid_public():
    return jsonify({"publicKey": VAPID_PUB_B64, "enabled": bool(VAPID_PRIV)})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    if not _notif_read_authed():
        return jsonify({"error": "unauthorized"}), 401
    data     = request.json or {}
    endpoint = str(data.get("endpoint", "")).strip()
    keys     = data.get("keys") or {}
    p256dh   = str(keys.get("p256dh", "")).strip()
    auth     = str(keys.get("auth", "")).strip()
    if not (endpoint and p256dh and auth):
        return jsonify({"error": "invalid_subscription"}), 400
    try:
        _push_add(endpoint, p256dh, auth, request.headers.get("User-Agent", "")[:300])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    if not _notif_read_authed():
        return jsonify({"error": "unauthorized"}), 401
    endpoint = str((request.json or {}).get("endpoint", "")).strip()
    if not endpoint:
        return jsonify({"error": "endpoint_required"}), 400
    try:
        _push_remove(endpoint)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── APK cookie auth (shared across all *.your-domain.example.com subdomains) ──────────
# Twin of the implementation in sentinel-vpn-dashboard/app.py and
# sentinel-smdl/app/miniapp.py. A signed cookie set by /auth/setup on the
# Suite launcher is honoured here too (Domain=.your-domain.example.com), so the APK
# bypasses Telegram-login + TOTP after a single one-time setup hop.
OWNER_AUTH_TOKEN = os.environ.get("OWNER_AUTH_TOKEN", "") or _secret("owner_auth_token", "OWNER_AUTH_TOKEN", "owner_auth_token")
APK_COOKIE_NAME  = "sentinel_apk_session"
APK_COOKIE_DOMAIN = ".your-domain.example.com"
APK_COOKIE_TTL   = 90 * 24 * 3600


def _verify_apk_cookie(val: str) -> bool:
    if not val or not OWNER_AUTH_TOKEN:
        return False
    try:
        body, sig = val.rsplit(".", 1)
        ts_s, _   = body.split(".", 1)
        expected  = hmac.new(OWNER_AUTH_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        return (time.time() - int(ts_s)) < APK_COOKIE_TTL
    except Exception:
        return False


def _issue_apk_cookie() -> str:
    ts    = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    body  = f"{ts}.{nonce}"
    sig   = hmac.new(OWNER_AUTH_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _safe_token_eq(a: str, b: str) -> bool:
    """Constant-time token comparison tolerant of pasted unicode (mobile
    keyboards). See sentinel-vpn-dashboard/app.py for the rationale."""
    if not a or not b:
        return False
    try:
        a_clean = "".join(ch for ch in a if 32 <= ord(ch) < 127)
        return hmac.compare_digest(a_clean.encode("utf-8"), b.encode("utf-8"))
    except Exception:
        return False


@app.route("/auth/setup", methods=["GET", "POST"])
def auth_setup():
    """One-time bootstrap from the Suite APK: validate owner token → set domain-
    wide APK cookie → bounce back to `/`. GET form lets a phone enter the token
    when the domain-wide cookie hasn't propagated. POST is the form submit."""
    if request.method == "POST":
        token = (request.form.get("token") or "").strip()
        nxt   = (request.form.get("next") or "/").strip()
        if not nxt.startswith("/"):
            nxt = "/"
        if not _safe_token_eq(token, OWNER_AUTH_TOKEN):
            return Response("Invalid token", status=401, mimetype="text/plain")
        host = request.host.split(":", 1)[0].lower()
        domain = APK_COOKIE_DOMAIN if host.endswith("your-domain.example.com") else None
        resp = Response("", status=303, headers={"Location": nxt})
        resp.set_cookie(
            APK_COOKIE_NAME,
            value=_issue_apk_cookie(),
            max_age=APK_COOKIE_TTL,
            domain=domain,
            path="/",
            secure=domain is not None,
            httponly=True,
            samesite="Lax",
        )
        return resp
    # GET — render a tiny fallback form so the user can paste their token here
    # if the Suite launcher's domain-wide cookie didn't make it to this host.
    nxt = request.args.get("next", "/")
    return Response(
        f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sentinel · Setup</title>
<style>body{{font:15px system-ui;background:#1c1c1e;color:#e8e8ea;max-width:380px;margin:60px auto;padding:0 22px;text-align:center}}
input{{width:100%;padding:14px;border-radius:12px;border:1px solid #38383a;background:#2c2c2e;color:#e8e8ea;font:15px monospace;outline:none}}
button{{width:100%;margin-top:14px;padding:14px;border:none;border-radius:12px;background:#2997ff;color:white;font-size:15px}}
.hint{{color:#636366;font-size:11px;margin-top:24px;line-height:1.5}}</style>
<div>⚡</div><h2>Sentinel · APK setup</h2>
<form method=POST action="/auth/setup">
<input name=token type=password placeholder="Owner token" autofocus>
<input type=hidden name=next value="{nxt}">
<button type=submit>Activate</button>
</form>
<div class=hint>Same token used on the Suite launcher.<br>Cookie persists 90 days on .your-domain.example.com.</div>""",
        mimetype="text/html",
    )


# ── API: Watchdog v2 proxy ────────────────────────────────────────────────────
# Thin proxies that forward to sentinel-watchdog v2 (:8200).
# Auth: reads the service token from sentinel-watchdog/config.json (live reload).
# All are session-gated on the bridge side (before_request fires first).

WATCHDOG_V2_URL = "http://127.0.0.1:8200"
_WATCHDOG_V2_CONFIG = REPO_ROOT / ".." / "sentinel-watchdog" / "config.json"


def _v2_token() -> str:
    """Read the v2 service token from sentinel-watchdog/config.json (live)."""
    try:
        with open(_WATCHDOG_V2_CONFIG, encoding="utf-8-sig") as _f:
            _data = json.load(_f)
        _tokens = _data.get("service_tokens") or {}
        return next(iter(_tokens.values()), "")
    except Exception:
        return ""


def _v2_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{WATCHDOG_V2_URL}{path}",
        headers={"X-Sentinel-Service-Token": _v2_token()},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def _v2_post(path: str, body: dict | None = None, *, timeout: int = 30) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"{WATCHDOG_V2_URL}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Sentinel-Service-Token": _v2_token(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


@app.route("/api/v2/pillars")
def api_v2_pillars():
    try:
        return jsonify(_v2_get("/api/v2/pillars"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/v2/pillars/<pillar_id>")
def api_v2_pillar_detail(pillar_id):
    if not _re.fullmatch(r"[a-z0-9_-]{1,40}", pillar_id):
        return jsonify({"error": "bad pillar_id"}), 400
    try:
        return jsonify(_v2_get(f"/api/v2/pillars/{pillar_id}"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/v2/pillars/<pillar_id>/restart", methods=["POST"])
def api_v2_pillar_restart(pillar_id):
    if not _re.fullmatch(r"[a-z0-9_-]{1,40}", pillar_id):
        return jsonify({"error": "bad pillar_id"}), 400
    try:
        # 90s: docker containers with timeout_seconds=60 can take up to ~75s
        return jsonify(_v2_post(f"/api/v2/pillars/{pillar_id}/restart", timeout=90))
    except urllib.error.HTTPError as e:
        return jsonify({"error": e.read().decode()[:200]}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/v2/services/<service_id>/restart", methods=["POST"])
def api_v2_service_restart(service_id):
    if not _re.fullmatch(r"[a-z0-9_.-]{1,60}", service_id):
        return jsonify({"error": "bad service_id"}), 400
    try:
        # 90s: docker containers with timeout_seconds=60 can take up to ~75s
        return jsonify(_v2_post(f"/api/v2/services/{service_id}/restart", timeout=90))
    except urllib.error.HTTPError as e:
        return jsonify({"error": e.read().decode()[:200]}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/v2/system")
def api_v2_system():
    try:
        return jsonify(_v2_get("/api/v2/system"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/v2/system/pillars")
def api_v2_system_pillars():
    include_disk = "1" if request.args.get("include_disk") == "1" else "0"
    qs = f"?include_disk={'true' if include_disk == '1' else 'false'}"
    try:
        return jsonify(_v2_get(f"/api/v2/system/pillars{qs}"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/v2/audit")
def api_v2_audit():
    limit = min(int(request.args.get("limit", 50)), 200)
    try:
        return jsonify(_v2_get(f"/api/v2/audit?limit={limit}"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── Shared Brain routes (Phase 4) ─────────────────────────────────────────────
# Mounts /api/brain/* on the Flask app. Auth is inherited from `before_request`.
def _mirror_chat_to_tg(user_msg: str, reply_text: str) -> None:
    """Cross-surface mirror: send a /chat exchange to the owner's TG DM so
    the conversation also lives in the TG history alongside bot-native
    turns. Two messages — user's question prefixed with [/chat], then
    Dove's reply prefixed with 🤖. Truncates to TG's 4096-char limit per
    message. Never raises; caller wraps in try/except too.

    The user-message direction is now owned by tg_user_mirror (Telethon),
    which posts the question AS the owner with no bot prefix — so we no
    longer echo the user's text here (that produced the duplicate
    "💬 [via /chat]" bot message). We keep the reply piece as a fallback so
    the assistant reply still lands in TG even if the Telethon/sidecar path
    is down."""
    if not (TELEGRAM_TOKEN and OWNER_ID):
        return
    MAX = 4000  # leave headroom under 4096
    pieces = []
    if reply_text:
        head = "🤖 "
        body = reply_text if len(reply_text) <= MAX - len(head) else reply_text[:MAX - len(head) - 1] + "…"
        pieces.append(head + body)
    for piece in pieces:
        try:
            payload = {"chat_id": str(OWNER_ID), "text": piece,
                       "disable_web_page_preview": True}
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            print(f"[mirror] sendMessage failed: {e}")
            break  # don't blast multiple errors


try:
    import brain_routes  # type: ignore
    brain_routes.register(
        app, owner_id=OWNER_ID, session_info_fn=_session_info,
        mirror_fn=_mirror_chat_to_tg,
    )
except Exception as _brain_exc:
    print(f"[brain] route registration failed: {_brain_exc}")


# ── Static ────────────────────────────────────────────────────────────────────
_INDEX_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")
_CHAT_PATH = os.path.join(os.path.dirname(__file__), "static", "chat.html")


@app.route("/chat")
def chat_page():
    """Phase 4 chat panel. Same SENTINEL_TOKEN injection trick as /."""
    if not os.path.exists(_CHAT_PATH):
        return Response("chat panel not yet built", status=404, mimetype="text/plain")
    with open(_CHAT_PATH, encoding="utf-8") as f:
        html = f.read()
    apk_token = _maybe_apk_session_token()
    injection = (f'<script>'
                 f'window.SENTINEL_TOKEN="{MINI_APP_SECRET}";'
                 f'window.BOT_USERNAME="{BOT_USERNAME}";'
                 f'window.APK_SESSION_TOKEN="{apk_token}";'
                 f'</script>')
    html = html.replace("</head>", f"{injection}\n</head>", 1)
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


def _maybe_apk_session_token() -> str:
    """If the request carries a valid APK cookie, mint a per-session session_token
    bound to the owner and return its hex value. Otherwise empty string. The
    front-end stores this in localStorage and skips the Telegram-login + TOTP."""
    if not _verify_apk_cookie(request.cookies.get(APK_COOKIE_NAME, "")):
        return ""
    ip = request.remote_addr or ""
    ua = (request.headers.get("User-Agent") or "")[:120]
    try:
        tok, _exp = _new_session(OWNER_ID, ip, ua + " apk-suite")
        return tok
    except Exception:
        return ""


@app.route("/")
def index():
    with open(_INDEX_PATH, encoding="utf-8") as f:
        html = f.read()
    apk_token = _maybe_apk_session_token()
    injection = (f'<script>'
                 f'window.SENTINEL_TOKEN="{MINI_APP_SECRET}";'
                 f'window.BOT_USERNAME="{BOT_USERNAME}";'
                 f'window.APK_SESSION_TOKEN="{apk_token}";'
                 f'</script>')
    html = html.replace("</head>", f"{injection}\n</head>", 1)
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


# ── #42 — /internal/reload-env (#27 fanout) ──────────────────────────────────
# Loopback + INTERNAL_RELOAD_TOKEN-gated. Called by sentinel-watchdog's
# secrets API after .env.local is updated, so the bridge can hot-swap
# OWNER_AUTH_TOKEN without an SC_StopThenStart cycle.
import _reload_env as _renv  # vendored from sentinel-watchdog @ c9d42cf


def _swap_owner_auth_token(v: str) -> None:
    """Rebind the module-level OWNER_AUTH_TOKEN so /api/auth/* and the
    APK cookie verifier pick up the new value on the very next request.
    The verifier reads it by name (no captured local), so this single
    rebind propagates across all sessions."""
    global OWNER_AUTH_TOKEN
    OWNER_AUTH_TOKEN = v


_renv.register_hot_swap("OWNER_AUTH_TOKEN", _swap_owner_auth_token)


@app.route("/internal/reload-env", methods=["POST"])
def internal_reload_env():
    host = request.remote_addr or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"detail": f"internal endpoint: loopback only (got {host})"}), 403
    expected = os.environ.get("INTERNAL_RELOAD_TOKEN", "")
    presented = request.headers.get("X-Internal-Reload-Token", "")
    if not expected:
        return jsonify({"detail": "INTERNAL_RELOAD_TOKEN not set in env"}), 503
    if not hmac.compare_digest(expected, presented):
        return jsonify({"detail": "internal endpoint: token mismatch"}), 401
    body = request.get_json(silent=True) or {}
    keys = body.get("keys") if isinstance(body, dict) else None
    env_path_str = os.environ.get(
        "ENV_LOCAL_PATH",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env.local")),
    )
    from pathlib import Path
    result = _renv.reload_env_in_process(Path(env_path_str), keys=keys)
    return jsonify({"ok": True, **result}), 200


if __name__ == "__main__":
    _write_totp_setup_page()
    def _purge_loop():
        while True:
            time.sleep(3600)
            _purge_expired()
    threading.Thread(target=_purge_loop, daemon=True).start()
    print("Sentinel Mini App v2 Bridge on :8098")
    app.run(host="127.0.0.1", port=8098, debug=False)
