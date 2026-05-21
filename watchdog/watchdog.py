"""
Sentinel Watchdog — owner-only Telegram management bot.
Runs as a native Windows process (Task Scheduler), independent of OpenClaw.
"""

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import keyring
import requests
from concurrent.futures import ThreadPoolExecutor

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import (  # noqa: E402
    TELEGRAM_PAIRING as _TG_PAIRING_PATH, SESSIONS_JSON as _SESSIONS_JSON_PATH,
    WSL_DISTRO as _WSL_DISTRO, OPENCLAW_NPM_BIN_BASH as _NPM_BIN,
    OPENCLAW_NPM_PKG_PATH_BASH as _NPM_PKG,
)

CONFIG_FILE      = Path(__file__).parent / "config.json"
CHECKPOINT_FILE  = Path(__file__).parent / "github_sync_checkpoint.json"
CONTACTS_FILE    = Path(__file__).parent / "contacts.json"
OPENCLAW_PAIRING = str(_TG_PAIRING_PATH)
MEMORY_MCP_URL   = "http://127.0.0.1:8092/mcp"
STATUS_PORT      = 8099
PLAYWRIGHT_SCREENSHOT_DIR = os.path.expandvars(r"%TEMP%\.playwright-mcp")
_KEYRING_SERVICE  = "sentinel-watchdog"
_KEYRING_USERNAME = "bot_token"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW  # suppress console popups on Windows
CALLBACK_MODEL   = "model:"
CALLBACK_RESTART = "restart:"
CALLBACK_POWER   = "power:"
CALLBACK_LOGS    = "logs:"

DISK_DRIVES = [
    ("C:\\", "System (C:)"),
    ("G:\\", "Downloads (G:)"),
]

DOCKER_EXE      = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
PLAYWRIGHT_TASK = "Playwright MCP Watcher"

# Restart backoff — shared between auto-restart and manual restart paths
_openclaw_last_restart: float = 0.0
OPENCLAW_RESTART_COOLDOWN = 60  # seconds


def _build_restart_map(cfg: dict) -> dict:
    """Build the auto-restart dispatch table from config paths."""
    return {
        "MetaMCP":              ("docker",   "metamcp"),
        "Reminders MCP":        ("docker",   "reminders-mcp"),
        "SMDL MCP":             ("docker",   "ytdlp-mcp"),
        "Google WS MCP":        ("docker",   "google-workspace-mcp"),
        "Maps MCP":             ("docker",   "maps-mcp"),
        "Memory MCP":           ("docker",   "memory-mcp"),
        "GitHub MCP":           ("docker",   "github-mcp"),
        "OneDrive MCP":         ("docker",   "onedrive-mcp"),
        "SMDL (s.)":            ("docker",   "smdl"),
        "Translate MCP":        ("docker",   "translate-mcp"),
        "Sentinel (OpenClaw)":  ("openclaw", None),
        "Infer Bridge":         ("proc",     cfg["infer_bridge"],    8095),
        "Sentinel Bridge":      ("proc",     cfg["sentinel_bridge"], 8098),
        "Shopping MCP":         ("proc",     cfg.get("shopping_mcp") or r"C:\Users\azfar\sentinel-shopping\mcp_server.py", 8100),
        "Playwright proxy :8932": ("task",   PLAYWRIGHT_TASK,        8932),
        "OpenClaw Sidepanel Bridge": ("proc", cfg.get("openclaw_bridge") or r"C:\Users\azfar\metamcp-local\comet-sidepanel\bridge.py", 8101),
        "Comet-Sidepanel MCP":  ("proc", cfg.get("openclaw_bridge_mcp") or r"C:\Users\azfar\metamcp-local\comet-sidepanel\mcp_server.py", 8102),
    }

_CB_KEY: bytes = b""  # set in Watchdog.__init__ from bot token; used by _cb_sign/_cb_verify

# ── Contact registry ──────────────────────────────────────────────────────────
_contacts_lock = threading.Lock()


def _load_contacts() -> dict:
    try:
        with open(CONTACTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_contacts(contacts: dict) -> None:
    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        json.dump(contacts, f, indent=2, ensure_ascii=False)


OPENCLAW_SESSIONS = str(_SESSIONS_JSON_PATH)


def _read_openclaw_pairing() -> list:
    """Read OpenClaw's pairing store and return contact dicts (chat_id, first_name, ...)."""
    try:
        with open(OPENCLAW_PAIRING, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for r in data.get("requests", []):
        chat_id = str(r.get("id", "")).strip()
        if not chat_id:
            continue
        meta = r.get("meta", {}) or {}
        out.append({
            "chat_id":       chat_id,
            "first_name":    meta.get("firstName", "") or "",
            "username":      meta.get("username",  "") or "",
            "registered_at": r.get("createdAt", "") or "",
            "source":        "openclaw",
        })
    return out


def _read_openclaw_sessions() -> list:
    """Pull contact info from OpenClaw's sessions.json (more reliable than the
    pairing store which clears after approval). Each session key is like
    'agent:main:telegram:direct:<chat_id>'; origin.label has 'Name (@username) id:<chat_id>'."""
    try:
        with open(OPENCLAW_SESSIONS, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for key, info in (data or {}).items():
        if ":telegram:" not in key or not isinstance(info, dict):
            continue
        chat_id = key.rsplit(":", 1)[-1]
        origin = info.get("origin", {}) or {}
        label = origin.get("label", "") or ""
        # Parse label — formats vary:
        #   "Name (@username) id:<chat_id>"  (full)
        #   "Name (@username)"               (no id suffix)
        #   "Name"                           (just the display name)
        first_name = ""
        username = ""
        if "(@" in label:
            first_name = label.split("(@", 1)[0].strip()
            username = label.split("(@", 1)[1].split(")")[0].strip()
        elif " id:" in label:
            first_name = label.split(" id:", 1)[0].strip()
        else:
            first_name = label.strip()
        out.append({
            "chat_id":       chat_id,
            "first_name":    first_name,
            "username":      username,
            "registered_at": "",
            "source":        "openclaw-session",
        })
    return out


def _merged_contacts(local: dict) -> list:
    """Merge local contacts.json with OpenClaw's pairing requests AND active sessions.
    OpenClaw is authoritative for any entry it has (sessions > pairing > local)."""
    merged = {k: dict(v) for k, v in local.items()}
    # Layer pairing on top (newer than local)
    for c in _read_openclaw_pairing():
        cid = c["chat_id"]
        existing = merged.get(cid, {})
        merged[cid] = {
            "chat_id":       cid,
            "first_name":    c["first_name"]    or existing.get("first_name", ""),
            "username":      c["username"]      or existing.get("username", ""),
            "registered_at": c["registered_at"] or existing.get("registered_at", ""),
        }
    # Layer sessions on top (most authoritative — survives after pairing-store clears)
    for c in _read_openclaw_sessions():
        cid = c["chat_id"]
        existing = merged.get(cid, {})
        merged[cid] = {
            "chat_id":       cid,
            "first_name":    c["first_name"]    or existing.get("first_name", ""),
            "username":      c["username"]      or existing.get("username", ""),
            "registered_at": existing.get("registered_at", ""),
        }
    return list(merged.values())


def _cb_sign(data: str) -> str:
    """Append 8-char HMAC suffix so callback payloads can't be forged."""
    mac = hmac.new(_CB_KEY, data.encode(), hashlib.sha256).hexdigest()[:8]
    return f"{data}.{mac}"


def _cb_verify(raw: str) -> str | None:
    """Verify suffix and return clean payload, or None on failure."""
    if "." not in raw:
        return None
    payload, mac = raw.rsplit(".", 1)
    expected = hmac.new(_CB_KEY, payload.encode(), hashlib.sha256).hexdigest()[:8]
    if hmac.compare_digest(expected, mac):
        return payload
    return None


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

    # Load secrets from Windows Credential Manager (never from config file)
    token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    if not token:
        print(
            "ERROR: bot token not found in Windows Credential Manager.\n"
            "Run  py store_secrets.py bot_token <token>  then restart the watchdog."
        )
        sys.exit(1)
    cfg["bot_token"] = token

    lm_key = keyring.get_password(_KEYRING_SERVICE, "lm_api_key")
    if lm_key:
        cfg["lm_studio_api_key"] = lm_key

    github_pat = keyring.get_password(_KEYRING_SERVICE, "github_pat")
    if github_pat:
        cfg["github_pat"] = github_pat
    elif not cfg.get("github_pat"):
        print("WARNING: github_pat not found in Credential Manager or config.json — GitHub sync disabled.")

    _stack_root = Path(__file__).parent.parent
    if not cfg.get("lm_studio_exe"):
        cfg["lm_studio_exe"] = str(Path.home() / "AppData/Local/Programs/LM Studio/LM Studio.exe")
    if not cfg.get("infer_bridge"):
        cfg["infer_bridge"] = str(_stack_root / "infer_bridge.py")
    if not cfg.get("sentinel_bridge"):
        cfg["sentinel_bridge"] = str(_stack_root / "sentinel-miniapp-v2/bridge.py")

    return cfg


# ── Helpers ───────────────────────────────────────────────────────────────────

def wsl(cmd: str, cfg: dict, timeout: int = 20) -> str:
    result = subprocess.run(
        ["wsl", "-d", cfg["wsl_distro"], "-u", "root", "--", "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    return (result.stdout + result.stderr).strip()


def docker_inspect(container: str, fmt: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", container, "--format", fmt],
        capture_output=True, text=True, timeout=10,
        creationflags=_NO_WINDOW,
    )
    return result.stdout.strip() if result.returncode == 0 else "not found"


def docker_restart(container: str, timeout: int = 30) -> bool:
    result = subprocess.run(
        ["docker", "restart", container],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    return result.returncode == 0


def port_open(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def icon(state) -> str:
    if state is True:
        return "🟢"
    if state == "warn":
        return "⚠️"
    return "🔴"


def _dns_resolve(host: str) -> tuple[bool, str]:
    try:
        addrs = socket.getaddrinfo(host, None, socket.AF_INET)
        if addrs:
            return True, addrs[0][4][0]
        return False, "no A records"
    except socket.gaierror as e:
        return False, str(e).split("]")[-1].strip()


def _https_check(url: str) -> tuple[bool, str]:
    try:
        r = requests.get(url, timeout=8, allow_redirects=True)
        return True, f"HTTP {r.status_code}"
    except requests.exceptions.SSLError:
        return False, "SSL error"
    except requests.exceptions.ConnectionError:
        return False, "connection refused"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:40]


def get_disk_snapshot() -> list[tuple[str, int, int, int]]:
    """Returns [(label, used_gb, total_gb, pct), ...]"""
    results = []
    for drive, label in DISK_DRIVES:
        try:
            u = shutil.disk_usage(drive)
            used  = u.used  // (1024 ** 3)
            total = u.total // (1024 ** 3)
            pct   = u.used * 100 // u.total
            results.append((label, used, total, pct))
        except Exception:
            results.append((label, -1, -1, -1))
    return results


def get_lm_info(api_key: str | None = None) -> str:
    """Return currently loaded model ID(s) from LM Studio, or status string.
    api_key arg is now legacy — we always pull fresh from WCM. The argument
    stays for backwards compatibility with callers that still pass it but
    its value is ignored. Phase B (2026-05-11) — see _check_http for the
    rationale on per-call key reads."""
    try:
        if keyring is not None:
            try:
                api_key = keyring.get_password(_KEYRING_SERVICE, "lm_api_key") or api_key
            except Exception:
                pass  # keep whatever was passed in
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = requests.get("http://localhost:1234/v1/models", headers=headers, timeout=5)
        if r.status_code == 200:
            models = r.json().get("data", [])
            if models:
                return ", ".join(m.get("id", "?") for m in models)
            return "no model loaded"
        return f"HTTP {r.status_code}"
    except requests.exceptions.ConnectionError:
        return "offline"
    except Exception as e:
        return str(e)[:40]


def _disk_lines(disk: list) -> list[str]:
    lines = []
    for label, used, total, pct in disk:
        if total < 0:
            lines.append(f"  {label}: unavailable")
        else:
            warn = " ⚠️" if pct > 85 else ""
            lines.append(f"  {label}: {used} GB / {total} GB ({pct}%){warn}")
    return lines


def _read_openclaw(cfg: dict) -> tuple[dict | None, str]:
    path = cfg.get("openclaw_config")
    if not path:
        return None, "openclaw_config not set in watchdog config.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), ""
    except Exception as e:
        return None, f"Could not read openclaw.json: {e}"


def _write_openclaw(cfg: dict, data: dict) -> str:
    path = cfg.get("openclaw_config")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return ""
    except Exception as e:
        return f"Failed to write openclaw.json: {e}"


# ── Health snapshot ───────────────────────────────────────────────────────────

MONITORED_CONTAINERS = [
    # ── AI Mini App layer + its MCP tools ───────────────────────────────
    ("metamcp",              "MetaMCP"),
    ("reminders-mcp",        "Reminders MCP"),
    ("ytdlp-mcp",            "SMDL MCP"),
    ("google-workspace-mcp", "Google WS MCP"),
    ("maps-mcp",             "Maps MCP"),
    ("memory-mcp",           "Memory MCP"),
    ("github-mcp",           "GitHub MCP"),
    ("onedrive-mcp",         "OneDrive MCP"),
    # ── Sentinel non-AI services + their dependencies ───────────────────
    ("smdl",                 "SMDL (s.)"),
    ("portfolio-mcp",        "Sentinel Finance"),
    ("vaultwarden",          "Vaultwarden"),
    ("firefly",              "Firefly III"),
    ("firefly-db",           "Firefly DB"),
    ("firefly-importer",     "Firefly Importer"),
    ("headscale",            "Headscale"),
    ("libretranslate",       "LibreTranslate"),
    ("metamcp-pg",           "MetaMCP Postgres"),
    ("pia-exit",             "PIA Exit (VPN)"),
]

# Tier 1: HTTP health endpoints (label, url) — localhost probes,
# confirm the service answers (not just that the container is alive).
HEALTH_ENDPOINTS = [
    ("MetaMCP",          "http://localhost:12008/health"),
    ("Reminders MCP",    "http://localhost:8087/health"),
    ("SMDL MCP",         "http://localhost:8088/health"),
    ("Google WS MCP",    "http://localhost:8089/health"),
    ("Maps MCP",         "http://localhost:8090/health"),
    ("Memory MCP",       "http://localhost:8092/health"),
    ("OneDrive MCP",     "http://localhost:8093/health"),
    ("Vaultwarden",      "http://localhost:8085/alive"),
    ("SMDL (s.)",        "http://localhost:8096/health"),
    ("Sentinel Finance", "http://localhost:8086/health"),
    ("LibreTranslate",   "http://localhost:5050/languages"),
    ("LM Studio API",    "http://localhost:1234/v1/models"),
    ("OpenClaw Sidepanel Bridge", "http://localhost:8101/health"),
]

# Tier 2: Port reachability (port, label)
MONITORED_PORTS = [
    (8091, "GitHub MCP :8091"),
    (8932, "Playwright proxy :8932"),
    (8095, "Infer Bridge"),
    (8098, "Sentinel Bridge"),
    (8100, "Shopping MCP"),
    (8094, "Translate MCP"),
    (8101, "OpenClaw Sidepanel Bridge"),
    (8102, "Comet-Sidepanel MCP"),
]

# Tier B (2026-05-16): Public Cloudflare Tunnel endpoints — end-to-end
# probe through DNS → Cloudflare edge → cloudflared agent → origin.
# Localhost /health passing while these fail = the tunnel layer is broken
# (cloudflared down, tunnel route misconfigured, CF edge outage, DNS drift,
# or TLS cert expired). This is the layer that matches your actual UX
# from a phone on cellular — and catches 502s like the one on 2026-05-16.
PUBLIC_ENDPOINTS = [
    ("Sentinel Finance (public)", "https://sentinelfinance.your-domain.example.com/health"),
    ("SMDL Mini App (public)",    "https://media.your-domain.example.com/health"),
    ("Sentinel AI Dash (public)", "https://your-domain.example.com/"),
    ("Vaultwarden (public)",      "https://vault.your-domain.example.com/alive"),
    ("Firefly III (public)",      "https://firefly.your-domain.example.com/"),
]


def _check_secret_drift(cfg: dict) -> list[tuple[str, str]]:
    """Phase D (2026-05-11) — meta-check that WCM canonical values match
    what's embedded in external consumer config files. Catches the class
    of bug where a rotation script forgot to update one of the file copies
    (the "two Tavily keys, only one rotated" pattern from earlier today).

    Returns a list of (label, detail) tuples for any drift detected.
    Empty list = no drift.

    Only checks consumers we can't refactor to per-probe reads:
      - openclaw.json (read-once-at-boot by openclaw-gateway, node)
      - MetaMCP postgres rows (read per MCP session, but only if
        container is restarted — env vars are container-immutable)
    """
    if keyring is None:
        return []  # can't compare against WCM
    drifts = []

    # 1. LM Studio key — WCM(sentinel-openclaw) vs openclaw.json (any sk-lm-)
    try:
        wcm_lm = keyring.get_password("sentinel-openclaw", "lmstudio_api_key") or ""
        oc_path = "/home/azfar/.openclaw/openclaw.json"
        oc_unc  = r"\\wsl.localhost\Ubuntu-24.04" + oc_path.replace("/", "\\")
        if os.path.exists(oc_unc) and wcm_lm:
            with open(oc_unc, encoding="utf-8-sig") as f:
                oc = json.load(f)
            file_lm_keys = set()
            def walk(d):
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k == "apiKey" and isinstance(v, str) and v.startswith("sk-lm-"):
                            file_lm_keys.add(v)
                        else:
                            walk(v)
                elif isinstance(d, list):
                    for x in d: walk(x)
            walk(oc)
            for fk in file_lm_keys:
                if fk != wcm_lm:
                    drifts.append(("Secret drift: LM Studio key", f"WCM ends ...{wcm_lm[-6:]} vs openclaw.json ends ...{fk[-6:]}"))
    except Exception as e:
        drifts.append(("Drift-check error: LM Studio", str(e)[:60]))

    # 2. Tavily key — WCM(sentinel-miniapp) vs openclaw.json (any tvly-)
    try:
        wcm_tv = keyring.get_password("sentinel-miniapp", "tavily_api_key") or ""
        if os.path.exists(oc_unc) and wcm_tv:
            file_tv_keys = set()
            def walk_tv(d):
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k == "apiKey" and isinstance(v, str) and v.startswith("tvly-"):
                            file_tv_keys.add(v)
                        else:
                            walk_tv(v)
                elif isinstance(d, list):
                    for x in d: walk_tv(x)
            walk_tv(oc)
            for fk in file_tv_keys:
                if fk != wcm_tv:
                    drifts.append(("Secret drift: Tavily key", f"WCM ends ...{wcm_tv[-6:]} vs openclaw.json ends ...{fk[-6:]}"))
    except Exception as e:
        drifts.append(("Drift-check error: Tavily", str(e)[:60]))

    # 3. Agent bot token — WCM(sentinel-miniapp/telegram_bot_token) vs
    #    openclaw.json:channels.telegram.botToken
    try:
        wcm_tg = keyring.get_password("sentinel-miniapp", "telegram_bot_token") or ""
        if os.path.exists(oc_unc) and wcm_tg:
            file_tg = oc.get("channels", {}).get("telegram", {}).get("botToken", "")
            if file_tg and file_tg != wcm_tg:
                drifts.append(("Secret drift: Agent bot token", f"WCM ends ...{wcm_tg[-6:]} vs openclaw.json ends ...{file_tg[-6:]}"))
    except Exception as e:
        drifts.append(("Drift-check error: Agent bot", str(e)[:60]))

    return drifts


def get_health_snapshot(cfg: dict) -> dict[str, bool]:
    def _check_container(container: str, label: str):
        health = docker_inspect(container, "{{.State.Health.Status}}")
        if health == "not found":
            state = docker_inspect(container, "{{.State.Status}}")
            ok = True if state == "running" else False
        elif health == "starting":
            ok = "warn"
        else:
            ok = True if health == "healthy" else False
        return label, ok

    svc = cfg["openclaw_service"]
    with ThreadPoolExecutor(max_workers=len(MONITORED_CONTAINERS) + 2) as pool:
        container_futs = [pool.submit(_check_container, c, l) for c, l in MONITORED_CONTAINERS]
        oc_fut  = pool.submit(wsl, f"systemctl is-active {svc}", cfg)
        lm_fut  = pool.submit(port_open, 1234)

    states = dict(f.result() for f in container_futs)
    states["Sentinel (OpenClaw)"] = (oc_fut.result().strip() == "active")
    states["LM Studio"] = lm_fut.result()
    return states


def get_connection_snapshot(cfg: dict) -> dict[str, tuple[bool, str]]:
    """Tier 1/2/3 deep connection checks. Returns {label: (ok, detail)}."""
    results = {}

    # Read LM Studio key fresh from WCM each probe cycle (Phase B —
    # cache-drift fix). Previously this was cached at boot via cfg, which
    # meant rotation scripts had to know to restart the watchdog or every
    # probe would fail 401 until manual restart. Now: rotation updates WCM,
    # next probe (≤30s away) picks up the new value automatically. The
    # try/except keeps the watchdog probing even if keyring is unreachable.
    lm_key = ""
    try:
        if keyring is not None:
            lm_key = keyring.get_password(_KEYRING_SERVICE, "lm_api_key") or ""
    except Exception:
        lm_key = cfg.get("lm_studio_api_key", "")  # fall back to boot-cached on keyring error

    # Tier 1: HTTP health probes — any HTTP response = up (conn refused/timeout = down)
    for label, url in HEALTH_ENDPOINTS:
        try:
            headers = {"Authorization": f"Bearer {lm_key}"} if (lm_key and "1234" in url) else {}
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                ok, detail = True, f"HTTP {r.status_code}"
            else:
                ok, detail = "warn", f"HTTP {r.status_code}"
        except requests.exceptions.ConnectionError:
            ok, detail = False, "connection refused"
        except requests.exceptions.Timeout:
            ok, detail = False, "timeout"
        except Exception as e:
            ok, detail = False, str(e)[:40]
        results[label] = (ok, detail)

    # Tier 2: Port reachability
    for port, label in MONITORED_PORTS:
        ok = port_open(port)
        results[label] = (ok, "listening" if ok else "no listener")

    # Tier B (2026-05-16): Public Cloudflare Tunnel probes. Hit the same
    # URLs a phone-on-cellular would hit, end-to-end. Catches tunnel-layer
    # outages that localhost probes miss.
    for label, url in PUBLIC_ENDPOINTS:
        try:
            r = requests.get(url, timeout=8, allow_redirects=True)
            if r.status_code == 200:
                ok, detail = True, f"HTTP {r.status_code}"
            elif r.status_code in (401, 403):
                # Auth-gated route reachable — the tunnel is up; auth wall
                # is the service's own behaviour, not a tunnel failure.
                ok, detail = True, f"HTTP {r.status_code} (auth-gated)"
            else:
                ok, detail = "warn", f"HTTP {r.status_code}"
        except requests.exceptions.SSLError:
            ok, detail = False, "SSL error (cert?)"
        except requests.exceptions.ConnectionError:
            ok, detail = False, "tunnel unreachable"
        except requests.exceptions.Timeout:
            ok, detail = False, "timeout"
        except Exception as e:
            ok, detail = False, str(e)[:40]
        results[label] = (ok, detail)

    # Tier 2.5: Secret drift meta-check (Phase D — 2026-05-11)
    # Compares WCM canonical values against what's embedded in external
    # consumer config files. Each drift becomes a "warn" result that
    # shows up in /status and triggers the alert path.
    for drift_label, drift_detail in _check_secret_drift(cfg):
        results[drift_label] = ("warn", drift_detail)

    # Tier 3: OpenClaw bundle-mcp log scan — scoped to current process PID only
    try:
        pid = wsl(
            f"systemctl show {cfg['openclaw_service']} --property=MainPID --value",
            cfg, timeout=5,
        ).strip()
        if pid and pid.isdigit() and pid != "0":
            out = wsl(
                f"journalctl _PID={pid} --no-pager -o short 2>/dev/null",
                cfg, timeout=12,
            )
            if "bundle-mcp" in out and "failed to start" in out:
                results["OpenClaw → MetaMCP"] = ("warn", "bundle-mcp failed")
            else:
                results["OpenClaw → MetaMCP"] = (True, "connected")
        else:
            results["OpenClaw → MetaMCP"] = (False, "OpenClaw not running")
    except Exception:
        results["OpenClaw → MetaMCP"] = (True, "log check skipped")

    return results


# ── Restart menus ─────────────────────────────────────────────────────────────

def _restart_main_menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "▶ Start full stack",              "callback_data": _cb_sign("power:confirm_start")}],
        [{"text": "⏹ Stop full stack",               "callback_data": _cb_sign("power:confirm_stop")}],
        [{"text": "↺ Restart AI Stack (containers)", "callback_data": _cb_sign("restart:aistack")}],
        [{"text": "↺ Sentinel  (OpenClaw)",          "callback_data": _cb_sign("restart:openclaw")}],
        [{"text": "↺ SMDL",                          "callback_data": _cb_sign("restart:smdl")}],
        [{"text": "Individual container ▸",          "callback_data": _cb_sign("restart:services")}],
    ]}


def _power_confirm_menu(action: str, label: str) -> dict:
    return {"inline_keyboard": [
        [{"text": f"Yes — {label}", "callback_data": _cb_sign(f"power:do_{action}")}],
        [{"text": "Cancel",         "callback_data": _cb_sign("restart:menu")}],
    ]}


def _restart_services_menu() -> dict:
    rows = []
    for container, label in MONITORED_CONTAINERS:
        if container == "smdl":
            continue  # smdl has its own top-level button
        rows.append([{"text": label, "callback_data": _cb_sign(f"restart:svc:{container}")}])
    rows.append([{"text": "◂ Back", "callback_data": _cb_sign("restart:menu")}])
    return {"inline_keyboard": rows}


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_help(_args: list[str], _cfg: dict):
    return (
        "<b>Sentinel Watchdog</b>\n\n"
        "/status — services, endpoints, disk, LM Studio model\n"
        "/restart — start/stop stack + restart menu\n"
        "/logs — choose container log source (inline menu)\n"
        "/logs [n] — OpenClaw journalctl, last N lines (max 50)\n"
        "/model — list models and switch with buttons\n"
        "/alerts — alert monitor status\n"
        "/dns — DNS propagation status for watched domains\n"
        "/sync — trigger GitHub → Memory sync now\n"
        "/dashboard — open the Sentinel Mini App\n"
        "/help — this message"
    )


def cmd_status(_args: list[str], cfg: dict):
    svc  = get_health_snapshot(cfg)
    conn = get_connection_snapshot(cfg)
    disk = get_disk_snapshot()
    lm   = get_lm_info(cfg.get("lm_studio_api_key"))

    lines = []

    # Docker containers
    lines.append("<b>Docker</b>")
    for _, label in MONITORED_CONTAINERS:
        lines.append(f"  {icon(svc.get(label, False))} {label}")

    # Processes: OpenClaw + port-monitored procs/tasks
    lines.append("\n<b>Processes</b>")
    lines.append(f"  {icon(svc.get('Sentinel (OpenClaw)', False))} Sentinel (OpenClaw)")
    oc_conn = conn.get("OpenClaw → MetaMCP")
    if oc_conn:
        ok, detail = oc_conn
        lines.append(f"  {icon(ok)} OpenClaw → MetaMCP: {detail}")
    for port, label in MONITORED_PORTS:
        ok, detail = conn.get(label, (False, "unknown"))
        lines.append(f"  {icon(ok)} {label} :{port} — {detail}")

    # HTTP health endpoints (localhost — origin reachable?)
    lines.append("\n<b>HTTP Endpoints</b>")
    for label, url in HEALTH_ENDPOINTS:
        ok, detail = conn.get(label, (False, "unknown"))
        lines.append(f"  {icon(ok)} {label}: {detail}")

    # Public tunnel endpoints (Cloudflare → cloudflared → origin —
    # end-to-end probes that match the phone-on-cellular path).
    lines.append("\n<b>Public Tunnel</b>")
    for label, url in PUBLIC_ENDPOINTS:
        ok, detail = conn.get(label, (False, "unknown"))
        lines.append(f"  {icon(ok)} {label}: {detail}")

    lines.append(f"\n<b>LM Studio</b>\n  Model: {lm}")

    lines.append("\n<b>Disk</b>")
    lines.extend(_disk_lines(disk))

    return "\n".join(lines)


def cmd_restart(_args: list[str], _cfg: dict):
    return "<b>What would you like to restart?</b>", _restart_main_menu()


def _model_friendly_names(oc: dict) -> dict[str, str]:
    """Map full model ID (provider/model-id) → friendly display name."""
    names = {}
    for provider, pdata in oc.get("models", {}).get("providers", {}).items():
        for m in pdata.get("models", []):
            names[f"{provider}/{m['id']}"] = m.get("name", m["id"])
    return names


def _model_provider_menu(primary: str) -> dict:
    lmstudio_active  = primary.startswith("lmstudio/")
    openrouter_active = primary.startswith("openrouter/")
    return {"inline_keyboard": [
        [{"text": ("✓ " if lmstudio_active  else "") + "LM Studio",
          "callback_data": _cb_sign("model:p:lmstudio")}],
        [{"text": ("✓ " if openrouter_active else "") + "OpenRouter  (free)",
          "callback_data": _cb_sign("model:p:openrouter")}],
    ]}


def _model_list_menu(oc: dict, provider: str) -> dict:
    registered = oc.get("agents", {}).get("defaults", {}).get("models", {})
    primary    = oc.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
    names      = _model_friendly_names(oc)

    rows = []
    for model_id in registered:
        if not model_id.startswith(f"{provider}/"):
            continue
        friendly = names.get(model_id, model_id.split("/")[-1])
        label    = f"✓ {friendly}" if model_id == primary else friendly
        rows.append([{"text": label, "callback_data": _cb_sign(f"model:set:{model_id}")}])
    rows.append([{"text": "◂ Back", "callback_data": _cb_sign("model:menu")}])
    return {"inline_keyboard": rows}


def cmd_model(_args: list[str], cfg: dict):
    oc, err = _read_openclaw(cfg)
    if err:
        return err
    primary = oc.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
    return "<b>Select provider:</b>", _model_provider_menu(primary)


def _do_switch_model(model_id: str, cfg: dict) -> str:
    oc, err = _read_openclaw(cfg)
    if err:
        return err

    registered = oc.get("agents", {}).get("defaults", {}).get("models", {})
    primary = oc.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")

    if model_id not in registered:
        return f"Model not registered: <code>{model_id}</code>"
    if model_id == primary:
        return f"Already active: <code>{model_id}</code>"

    oc["agents"]["defaults"]["model"]["primary"] = model_id
    err = _write_openclaw(cfg, oc)
    if err:
        return err

    svc = cfg["openclaw_service"]
    out = wsl(f"systemctl restart {svc} && sleep 3 && systemctl is-active {svc}", cfg, timeout=30)
    ok = out.strip() == "active"
    return f"Switched to <code>{model_id}</code>\nRestart: {'done' if ok else f'FAILED ({out})'}"


def _logs_menu() -> dict:
    rows = [[{"text": "OpenClaw (journalctl)", "callback_data": _cb_sign("logs:openclaw:20")}]]
    for container, label in MONITORED_CONTAINERS:
        rows.append([{"text": label, "callback_data": _cb_sign(f"logs:docker:{container}")}])
    return {"inline_keyboard": rows}


def cmd_logs(args: list[str], cfg: dict):
    if args and args[0].isdigit():
        # Numeric arg → OpenClaw journalctl (backward compat)
        n = min(int(args[0]), 50)
        out = wsl(
            f"journalctl -u {cfg['openclaw_service']} -n {n} --no-pager -o short",
            cfg, timeout=15,
        )
        if len(out) > 3800:
            out = "[truncated]\n..." + out[-3800:]
        return f"<pre>{out}</pre>"
    # No arg → show source menu
    return "<b>Choose log source:</b>", _logs_menu()


def cmd_dashboard(_args: list[str], cfg: dict):
    url = cfg.get("mini_app_url", "").strip()
    if not url:
        # fall back to sentinel_config.json next to the stack root
        sentinel_cfg = Path(__file__).parent.parent / "sentinel_config.json"
        try:
            url = json.loads(sentinel_cfg.read_text(encoding="utf-8")).get("mini_app_url", "")
        except Exception:
            url = ""
    if not url:
        return "mini_app_url not configured."
    return (
        "🖥️ <b>Sentinel Dashboard</b>",
        {"inline_keyboard": [[{"text": "Open Dashboard", "url": url}]]},
    )


# ── Dispatcher ────────────────────────────────────────────────────────────────

COMMANDS = {
    "help":      cmd_help,
    "status":    cmd_status,
    "restart":   cmd_restart,
    "model":     cmd_model,
    "logs":      cmd_logs,
    "dashboard": cmd_dashboard,
}


# ── Alert monitor ─────────────────────────────────────────────────────────────

class AlertMonitor:
    def __init__(self, bot: "Watchdog"):
        self.bot = bot
        self.cfg = bot.cfg
        self.interval = int(self.cfg.get("alert_interval_seconds", 300))
        self._last: dict[str, bool] = {}
        self._first_run = True
        self._down_since: dict[str, float] = {}
        self._oc_dupe_alerted = False
        self.restart_map = _build_restart_map(self.cfg)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="alert-monitor")

    def start(self):
        self._thread.start()
        print(f"Alert monitor started (interval: {self.interval}s)")

    def status_text(self) -> str:
        interval_min = self.interval // 60
        if not self._last:
            return f"Alert monitor running — no baseline yet (checks every {interval_min}m)"
        down = [svc for svc, ok in self._last.items() if not ok]
        if down:
            return f"Alert monitor active — {len(down)} service(s) DOWN:\n" + "\n".join(f"  • {s}" for s in down)
        return f"Alert monitor active — all services UP (checks every {interval_min}m)"

    def _loop(self):
        while True:
            try:
                self._check()
            except Exception as e:
                print(f"Alert monitor error: {e}")
            time.sleep(self.interval)

    def _auto_restart(self, label: str, kind: str, name: str | None, port: int | None = None):
        self.bot.send(self.bot.owner, f"🔄 Auto-restart: <b>{label}</b>...")
        try:
            if kind == "docker":
                ok = docker_restart(name)
                result = "restarted." if ok else "FAILED."
            elif kind == "proc":
                # Prefer a per-project venv if one sits alongside the script.
                # Falls back to `py -3` for legacy scripts (infer_bridge,
                # sentinel-miniapp-v2/bridge.py — system-Python deps only).
                script_dir = os.path.dirname(name) if name else ""
                venv_py = os.path.join(script_dir, ".venv", "Scripts", "python.exe")
                if script_dir and os.path.exists(venv_py):
                    cmd = [venv_py, "-u", name]
                else:
                    cmd = ["py", "-3", "-u", name]
                subprocess.Popen(
                    cmd,
                    cwd=script_dir or None,
                    creationflags=_NO_WINDOW | subprocess.DETACHED_PROCESS,
                )
                time.sleep(3)
                ok = port_open(port) if port else False
                result = "restarted." if ok else f"FAILED (port {port} still closed)."
            elif kind == "task":
                subprocess.run(
                    ["schtasks", "/run", "/tn", name],
                    creationflags=_NO_WINDOW, timeout=10,
                )
                time.sleep(5)
                ok = port_open(port) if port else True
                result = "task triggered." if ok else f"task triggered but port {port} still closed."
            else:  # openclaw
                global _openclaw_last_restart
                now = time.time()
                elapsed = now - _openclaw_last_restart
                if elapsed < OPENCLAW_RESTART_COOLDOWN:
                    remaining = int(OPENCLAW_RESTART_COOLDOWN - elapsed)
                    result = f"skipped — last attempt {int(elapsed)}s ago (cooldown {OPENCLAW_RESTART_COOLDOWN}s, {remaining}s left)"
                    ok = False
                else:
                    _openclaw_last_restart = now
                    svc = self.cfg["openclaw_service"]
                    out = wsl(
                        f"systemctl reset-failed {svc}; systemctl restart {svc} && sleep 3 && systemctl is-active {svc}",
                        self.cfg, timeout=35,
                    )
                    ok = out.strip() == "active"
                    result = "restarted." if ok else f"FAILED ({out})."
            self.bot.send(self.bot.owner, f"🔄 Auto-restart <b>{label}</b>: {result}")
        except Exception as e:
            self.bot.send(self.bot.owner, f"🔄 Auto-restart <b>{label}</b>: error — {e}")

    def _check(self):
        svc  = get_health_snapshot(self.cfg)
        conn = get_connection_snapshot(self.cfg)

        # Merge into flat bool dict for transition tracking ("warn" counts as not-ok)
        current: dict[str, bool] = {k: (v is True) for k, v in svc.items()}
        for label, (ok, detail) in conn.items():
            ok_bool = ok is True
            current[label] = ok_bool
            if not ok_bool:
                suffix = " [config]" if ok == "warn" else ""
                current[f"{label}_detail"] = detail + suffix

        if self._first_run:
            self._last = current
            self._first_run = False
            down = [s for s, v in current.items() if not s.endswith("_detail") and not v]
            if down:
                lines = []
                for s in down:
                    detail = current.get(f"{s}_detail", "")
                    lines.append(f"  • {s}" + (f" ({detail})" if detail else ""))
                msg = "<b>Watchdog started — issues detected:</b>\n" + "\n".join(lines)
                self.bot.send(self.bot.owner, msg)

                # On startup, auto-restart only the "proc"-type entries (bridges).
                # Docker/OpenClaw/task entries are deliberately skipped here — they
                # may still be initialising or intentionally off. User can trigger
                # those manually via Telegram /restart if needed.
                if self.cfg.get("auto_restart", False):
                    for label in down:
                        info = self.restart_map.get(label)
                        if info and info[0] == "proc":
                            kind, name = info[0], info[1]
                            port = info[2] if len(info) > 2 else None
                            threading.Thread(
                                target=self._auto_restart,
                                args=(label, kind, name, port),
                                daemon=True, name=f"autorestart-firstrun-{label}",
                            ).start()
            return

        went_down_labels, came_up = [], []
        for s, ok in current.items():
            if s.endswith("_detail"):
                continue
            was_ok = self._last.get(s, True)
            if not ok and was_ok:
                went_down_labels.append(s)
            elif ok and not was_ok:
                came_up.append(f"  • {s}")

        if went_down_labels:
            lines = []
            for s in went_down_labels:
                detail = current.get(f"{s}_detail", "")
                lines.append(f"  • {s}" + (f" ({detail})" if detail else ""))
                if s not in self._down_since:
                    self._down_since[s] = time.time()
            self.bot.send(self.bot.owner,
                "<b>CONNECTION DOWN</b>\n" + "\n".join(lines))
            if self.cfg.get("auto_restart", False):
                for label in went_down_labels:
                    restart_info = self.restart_map.get(label)
                    if restart_info:
                        kind = restart_info[0]
                        name = restart_info[1]
                        port = restart_info[2] if len(restart_info) > 2 else None
                        threading.Thread(
                            target=self._auto_restart, args=(label, kind, name, port),
                            daemon=True, name=f"autorestart-{label}",
                        ).start()

        if came_up:
            self.bot.send(self.bot.owner,
                "<b>CONNECTION RECOVERED</b>\n" + "\n".join(came_up))
            for s in came_up:
                self._down_since.pop(s.strip().lstrip("• "), None)

        # 10-minute escalation for anything still down
        now = time.time()
        for label, since in list(self._down_since.items()):
            if current.get(label) is not False:
                self._down_since.pop(label, None)
            elif now - since > 600:
                self.bot.send(self.bot.owner,
                    f"⚠️ <b>{label}</b> still down after 10+ min — check manually")
                self._down_since[label] = now  # reset timer so it fires every 10 min

        self._last = current
        self._check_port_dupes()
        self._check_openclaw_dupes()

    def _pids_on_port(self, port: int) -> list[int]:
        try:
            out = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW,
            ).stdout
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
                    try:
                        pids.add(int(parts[4]))
                    except ValueError:
                        pass
            return list(pids)
        except Exception:
            return []

    def _check_port_dupes(self):
        for port, label in MONITORED_PORTS:
            pids = self._pids_on_port(port)
            if len(pids) <= 1:
                continue
            keep   = min(pids)   # oldest PID = lowest number
            extras = [p for p in pids if p != keep]
            killed, failed = [], []
            for pid in extras:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, timeout=5,
                        creationflags=_NO_WINDOW,
                    )
                    killed.append(pid)
                except Exception:
                    failed.append(pid)
            msg = (
                f"⚠️ <b>Port conflict :{port} ({label})</b>\n"
                f"{len(pids)} processes competing — PIDs: {', '.join(str(p) for p in sorted(pids))}\n"
                f"Kept PID {keep}, killed: {', '.join(str(p) for p in killed)}"
            )
            if failed:
                msg += f"\nFailed to kill: {', '.join(str(p) for p in failed)}"
            self.bot.send(self.bot.owner, msg)

    def _check_openclaw_dupes(self):
        svc = self.cfg.get("openclaw_service", "openclaw-gateway")
        sys_active = wsl(f"systemctl is-active {svc} 2>/dev/null", self.cfg, timeout=8).strip() == "active"
        user_active = wsl(
            "runuser -l azfar -c 'systemctl --user is-active openclaw-gateway 2>/dev/null' 2>/dev/null",
            self.cfg, timeout=8,
        ).strip() == "active"

        if sys_active and user_active:
            if not self._oc_dupe_alerted:
                self._oc_dupe_alerted = True
                self.bot.send(
                    self.bot.owner,
                    "⚠️ <b>OpenClaw duplicate service detected</b>\n"
                    "Both user-level and system-level <code>openclaw-gateway</code> units are active — "
                    "this causes a kill loop.\n\n"
                    "Fix:\n"
                    "<code>systemctl --user stop openclaw-gateway</code>\n"
                    "<code>systemctl --user disable openclaw-gateway</code>",
                )
        else:
            self._oc_dupe_alerted = False


# ── Daily digest ─────────────────────────────────────────────────────────────

class DigestScheduler:
    def __init__(self, bot: "Watchdog", digest_time: str = "08:00"):
        h, m = map(int, digest_time.split(":"))
        self.bot        = bot
        self.hour       = h
        self.minute     = m
        self._last_date = None
        self._thread    = threading.Thread(
            target=self._loop, daemon=True, name="digest")

    def start(self):
        self._thread.start()
        print(f"Digest scheduler started (daily at {self.hour:02d}:{self.minute:02d})")

    def _loop(self):
        while True:
            now   = datetime.now()
            today = now.date()
            if (now.hour == self.hour and now.minute == self.minute
                    and today != self._last_date):
                try:
                    self._send()
                    self._last_date = today
                except Exception as e:
                    print(f"Digest error: {e}")
            time.sleep(60)

    def _send(self):
        cfg  = self.bot.cfg
        svc  = get_health_snapshot(cfg)
        conn = get_connection_snapshot(cfg)
        disk = get_disk_snapshot()
        lm   = get_lm_info(cfg.get("lm_studio_api_key"))

        all_ok = all(v is True for v in svc.values()) and all(ok is True for ok, _ in conn.values())
        header = "✅ All systems healthy" if all_ok else "⚠️ Issues detected"

        lines = [f"<b>Good morning! Daily Digest — {header}</b>",
                 f"<i>{datetime.now().strftime('%A, %d %b %Y')}</i>\n"]

        lines.append("<b>Services</b>")
        for label, ok in svc.items():
            lines.append(f"{icon(ok)} {label}")

        lines.append("\n<b>LM Studio</b>")
        lines.append(f"  Model: {lm}")

        lines.append("\n<b>Disk</b>")
        lines.extend(_disk_lines(disk))

        self.bot.send(self.bot.owner, "\n".join(lines))


# ── DNS propagation monitor ───────────────────────────────────────────────────

class DnsMonitor:
    """Checks domain DNS/HTTPS every 15 min and notifies until all resolve."""

    CHECK_INTERVAL = 900  # 15 minutes

    def __init__(self, bot: "Watchdog"):
        self.bot      = bot
        self.cfg      = bot.cfg
        self._domains = self.cfg.get("dns_watch", [])
        self._resolved: set[str] = set()
        self._check_count = 0
        self._done    = False
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="dns-monitor")

    def start(self):
        if not self._domains:
            return
        # Immediate first check so the user sees current status right away
        threading.Thread(target=self._check, daemon=True, name="dns-initial").start()
        self._thread.start()
        print(f"DNS monitor started — watching: {', '.join(self._domains)}")

    def current_status(self) -> str:
        if not self._domains:
            return "DNS monitor: no domains configured (add dns_watch to config.json)"
        lines = ["<b>🌐 DNS Status</b>"]
        for domain in self._domains:
            dns_ok, dns_detail   = _dns_resolve(domain)
            https_ok, https_detail = _https_check(f"https://{domain}")
            lines.append(
                f"\n{'🟢' if dns_ok else '🔴'} <code>{domain}</code> → {dns_detail}"
                f"\n  {'🟢' if https_ok else '🔴'} HTTPS: {https_detail}"
            )
        done_note = "\n<i>All resolved — periodic checks stopped.</i>" if self._done else \
                    f"\n<i>Checking every 15m (check #{self._check_count} so far)</i>"
        lines.append(done_note)
        return "\n".join(lines)

    def _loop(self):
        while not self._done:
            time.sleep(self.CHECK_INTERVAL)
            if not self._done:
                try:
                    self._check()
                except Exception as e:
                    print(f"DNS monitor error: {e}")

    def _check(self):
        self._check_count += 1
        domain_results = []
        newly_resolved = []
        all_dns_ok = True

        for domain in self._domains:
            dns_ok,   dns_detail   = _dns_resolve(domain)
            https_ok, https_detail = _https_check(f"https://{domain}")
            domain_results.append((domain, dns_ok, dns_detail, https_ok, https_detail))

            if dns_ok and domain not in self._resolved:
                newly_resolved.append((domain, dns_detail))
                self._resolved.add(domain)

            if not dns_ok:
                all_dns_ok = False

        # Status message
        lines = [f"<b>🌐 DNS Check #{self._check_count}</b>"]
        for domain, dns_ok, dns_detail, https_ok, https_detail in domain_results:
            lines.append(
                f"\n{'🟢' if dns_ok else '🔴'} <code>{domain}</code> → {dns_detail}"
                f"\n  {'🟢' if https_ok else '🔴'} HTTPS: {https_detail}"
            )
        if not all_dns_ok:
            lines.append("\n<i>Still propagating — retry in 15m</i>")
        self.bot.send(self.bot.owner, "\n".join(lines))

        # Celebrate newly resolved domains
        for domain, ip in newly_resolved:
            self.bot.send(
                self.bot.owner,
                f"🎉 <b>DNS LIVE!</b> <code>{domain}</code>\n"
                f"Resolved → <code>{ip}</code>",
            )

        # Stop periodic checks once every domain has an A record
        if all_dns_ok and len(self._resolved) == len(self._domains):
            self._done = True


# ── Memory MCP helper ────────────────────────────────────────────────────────

_mcp_session:  str | None = None
_mcp_lock = threading.Lock()


def _ensure_mcp_session() -> str | None:
    global _mcp_session
    with _mcp_lock:
        if _mcp_session:
            return _mcp_session
        try:
            r = requests.post(
                MEMORY_MCP_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "watchdog", "version": "1.0"}}},
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                timeout=10,
            )
            sid = r.headers.get("mcp-session-id", "")
            if sid:
                _mcp_session = sid
                return sid
        except Exception as e:
            print(f"[memory] MCP session error: {e}")
        return None


def _memory_store(content: str, tags: list[str] | None = None) -> bool:
    global _mcp_session
    for attempt in range(2):
        sid = _ensure_mcp_session()
        if not sid:
            return False
        try:
            r = requests.post(
                MEMORY_MCP_URL,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "memory_store",
                                 "arguments": {"content": content,
                                               "tags": tags or []}}},
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream",
                         "mcp-session-id": sid},
                timeout=15,
            )
            for line in r.text.splitlines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    return "error" not in data.get("result", {})
            return True
        except Exception as e:
            with _mcp_lock:
                _mcp_session = None
            if attempt == 1:
                print(f"[memory] store error: {e}")
    return False


# ── GitHub Syncer ─────────────────────────────────────────────────────────────

class GitHubSyncer:
    """Polls GitHub for issues and commits, stores summaries in MCP Memory.
    Checkpoint file ensures crash recovery — picks up from last synced position."""

    def __init__(self, cfg: dict):
        self.repo     = cfg.get("github_repo", "")
        self.pat      = cfg.get("github_pat", "")
        self.interval = int(cfg.get("github_sync_interval", 300))
        self._cp      = self._load_checkpoint()
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="github-sync")

    def start(self):
        if not self.repo or not self.pat:
            print("[github-sync] skipped — set github_repo and github_pat in config.json")
            return
        self._thread.start()
        print(f"[github-sync] started (repo: {self.repo}, interval: {self.interval}s)")

    def trigger(self):
        threading.Thread(target=self._sync, daemon=True, name="github-sync-manual").start()

    def _load_checkpoint(self) -> dict:
        try:
            with open(CHECKPOINT_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_checkpoint(self):
        try:
            with open(CHECKPOINT_FILE, "w") as f:
                json.dump(self._cp, f, indent=2)
        except Exception as e:
            print(f"[github-sync] checkpoint save failed: {e}")

    def _gh(self, path: str, params: dict | None = None):
        try:
            r = requests.get(
                f"https://api.github.com/repos/{self.repo}{path}",
                headers={"Authorization": f"Bearer {self.pat}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
                params=params or {},
                timeout=15,
            )
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            print(f"[github-sync] GET {path} error: {e}")
            return None

    def _loop(self):
        while True:
            try:
                self._sync()
            except Exception as e:
                print(f"[github-sync] sync error: {e}")
            time.sleep(self.interval)

    def _sync(self):
        self._sync_issues()
        self._sync_commits()
        self._save_checkpoint()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _sync_issues(self):
        since  = self._cp.get("issues_since", "2026-01-01T00:00:00Z")
        issues = self._gh("/issues", {"state": "all", "since": since, "per_page": 50,
                                      "sort": "updated", "direction": "asc"})
        if not issues:
            return
        repo_name = self.repo.split("/")[-1]
        count = 0
        for issue in issues:
            if "pull_request" in issue:
                continue  # skip PRs (they also appear in /issues)
            num    = issue["number"]
            title  = issue["title"]
            state  = issue["state"].upper()
            body   = (issue.get("body") or "")[:400].strip()
            labels = [l["name"] for l in issue.get("labels", [])]
            updated = issue["updated_at"]
            content = f"GitHub Issue #{num} [{state}]: {title}\nRepo: {self.repo}\nUpdated: {updated}"
            if labels:
                content += f"\nLabels: {', '.join(labels)}"
            if body:
                content += f"\n\n{body}"
            tags = ["github", "issue", state.lower(), repo_name]
            _memory_store(content, tags)
            count += 1
        if count:
            print(f"[github-sync] synced {count} issue(s)")
        self._cp["issues_since"] = self._now_iso()

    def _sync_commits(self):
        since   = self._cp.get("commits_since", "2026-01-01T00:00:00Z")
        commits = self._gh("/commits", {"since": since, "per_page": 20})
        if not commits:
            return
        repo_name = self.repo.split("/")[-1]
        count = 0
        for commit in commits:
            sha    = commit["sha"][:8]
            msg    = commit["commit"]["message"].split("\n")[0][:200]
            author = commit["commit"]["author"]["name"]
            date   = commit["commit"]["author"]["date"]
            content = (f"GitHub Commit [{sha}]: {msg}\n"
                       f"Repo: {self.repo}\nAuthor: {author} at {date}")
            tags = ["github", "commit", repo_name]
            _memory_store(content, tags)
            count += 1
        if count:
            print(f"[github-sync] synced {count} commit(s)")
        self._cp["commits_since"] = self._now_iso()


# ── Version checking ──────────────────────────────────────────────────────────

def _pip_ver(container: str, package: str) -> str:
    """Return installed pip package version from inside a docker container."""
    try:
        r = subprocess.run(
            ["docker", "exec", container, "pip", "show", package],
            capture_output=True, text=True, timeout=12, creationflags=_NO_WINDOW,
        )
        for line in (r.stdout + r.stderr).splitlines():
            if line.lower().startswith("version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def _container_file_ver(container: str, path: str) -> str:
    """Read a version file from inside a docker container."""
    try:
        r = subprocess.run(
            ["docker", "exec", container, "cat", path],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _gh_latest(owner: str, repo: str) -> str:
    """Return latest GitHub release tag (without leading v)."""
    try:
        r = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
            timeout=8, headers={"Accept": "application/vnd.github+json"},
        )
        return r.json().get("tag_name", "").lstrip("v")
    except Exception:
        return ""


def _npm_latest(package: str) -> str:
    """Return latest version of an npm package from the registry."""
    try:
        r = requests.get(f"https://registry.npmjs.org/{package}/latest", timeout=8)
        return r.json().get("version", "")
    except Exception:
        return ""


def _openclaw_ver(cfg: dict) -> str:
    """Read the openclaw npm package version from WSL."""
    distro = cfg.get("wsl_distro", "Ubuntu-24.04")
    script = (f"console.log(JSON.parse(require('fs').readFileSync("
              f"'{_NPM_PKG}'"
              f")).version)")
    try:
        r = subprocess.run(
            ["wsl", "-d", distro, "-u", "root", "--", "bash", "-c", f'node -e "{script}"'],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _container_label_ver(container: str) -> str:
    """Return org.opencontainers.image.version OCI label from a container."""
    try:
        r = subprocess.run(
            ["docker", "inspect", container,
             "--format", '{{index .Config.Labels "org.opencontainers.image.version"}}'],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _lmstudio_ver(exe_path: str | None = None) -> str:
    """Read LM Studio version from the Windows exe."""
    try:
        path = exe_path or str(Path.home() / "AppData/Local/Programs/LM Studio/LM Studio.exe")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'(Get-Item "{path}").VersionInfo.ProductVersion'],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW,
        )
        return r.stdout.strip().split("+")[0]  # strip build metadata if present
    except Exception:
        return ""


def _docker_desktop_ver() -> str:
    """Read Docker Desktop version from the Windows exe."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'(Get-Item "{DOCKER_EXE}").VersionInfo.ProductVersion'],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW,
        )
        ver = r.stdout.strip().split("+")[0]
        # ProductVersion is a 4-part Windows build number (e.g. 4.46.0.175994).
        # Strip the 4th segment so it compares cleanly against the 3-part release tag.
        parts = ver.split(".")
        return ".".join(parts[:3]) if len(parts) > 3 else ver
    except Exception:
        return ""


def _docker_desktop_latest() -> str:
    """Scrape latest Docker Desktop version from the release notes page."""
    import re
    try:
        r = requests.get(
            "https://docs.docker.com/desktop/release-notes/",
            timeout=10, headers={"User-Agent": "sentinel-watchdog/1.0"},
        )
        m = re.search(r"Docker Desktop[\s]+(\d+\.\d+\.\d+)", r.text)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _docker_pull_restart(image: str, container: str, timeout: int = 120) -> str:
    pull = subprocess.run(
        ["docker", "pull", image],
        capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
    )
    rst = subprocess.run(
        ["docker", "restart", container],
        capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
    )
    return (pull.stdout + pull.stderr + rst.stdout + rst.stderr).strip()[-400:]


def _run_update(update_id: str, cfg: dict | None = None) -> str:
    """Run the update command for the given component. Returns stdout or error."""
    pip_updates = {
        "ytdlp":  ("ytdlp-mcp", "yt-dlp"),
        "galldl": ("ytdlp-mcp", "gallery-dl"),
    }
    pull_updates = {
        "libretranslate": ("libretranslate/libretranslate:latest", "libretranslate"),
        "metamcp":        ("ghcr.io/metatool-ai/metamcp:latest",   "metamcp"),
        "github-mcp":     ("ghcr.io/github/github-mcp-server",     "github-mcp"),
    }
    if update_id in pip_updates:
        container, pkg = pip_updates[update_id]
        try:
            r = subprocess.run(
                ["docker", "exec", container, "pip", "install", "-U", pkg],
                capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
            )
            return (r.stdout + r.stderr).strip()[-400:] or "done"
        except Exception as e:
            return str(e)
    if update_id in pull_updates:
        image, container = pull_updates[update_id]
        return _docker_pull_restart(image, container)
    if update_id == "openclaw" and cfg:
        from _paths import OPENCLAW_NPM_PREFIX_BASH as _NPM_PREFIX
        distro = cfg.get("wsl_distro", _WSL_DISTRO)
        cmd = (
            f"npm install -g --prefix {_NPM_PREFIX} openclaw@latest"
            " && systemctl restart openclaw-gateway.service"
        )
        try:
            r = subprocess.run(
                ["wsl", "-d", distro, "-u", "root", "--", "bash", "-c", cmd],
                capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW,
            )
            return (r.stdout + r.stderr).strip()[-400:] or "done"
        except Exception as e:
            return str(e)
    return f"Unknown component: {update_id}"


def get_versions_snapshot(cfg: dict | None = None) -> list[dict]:
    """Parallel version checks for all tracked components."""
    with ThreadPoolExecutor(max_workers=14) as pool:
        # Current versions
        f_ytdlp_cur    = pool.submit(_pip_ver,             "ytdlp-mcp",     "yt-dlp")
        f_galldl_cur   = pool.submit(_pip_ver,             "ytdlp-mcp",     "gallery-dl")
        f_lt_cur       = pool.submit(_container_file_ver,  "libretranslate", "/app/VERSION")
        f_meta_cur     = pool.submit(_container_label_ver, "metamcp")
        f_gh_mcp_cur   = pool.submit(_container_label_ver, "github-mcp")
        f_lm_cur       = pool.submit(_lmstudio_ver, (cfg or {}).get("lm_studio_exe"))
        f_dd_cur       = pool.submit(_docker_desktop_ver)
        f_oc_cur       = pool.submit(_openclaw_ver, cfg or {})
        # Latest versions
        f_ytdlp_lat    = pool.submit(_gh_latest,  "yt-dlp",        "yt-dlp")
        f_galldl_lat   = pool.submit(_gh_latest,  "mikf",          "gallery-dl")
        f_lt_lat       = pool.submit(_gh_latest,  "LibreTranslate", "LibreTranslate")
        f_meta_lat     = pool.submit(_gh_latest,  "metatool-ai",   "metamcp")
        f_gh_mcp_lat   = pool.submit(_gh_latest,  "github",        "github-mcp-server")
        f_lm_lat       = pool.submit(_gh_latest,  "lmstudio-ai",   "lmstudio")
        f_dd_lat       = pool.submit(_docker_desktop_latest)
        f_oc_lat       = pool.submit(_npm_latest, "openclaw")

    def _norm(v: str) -> str:
        try: return ".".join(str(int(p)) for p in v.split("."))
        except ValueError: return v.lower().lstrip("v")

    def _item(name, cur, lat, uid):
        cur = cur or ""
        lat = lat or ""
        return {
            "name": name, "current": cur or "—", "latest": lat or "—",
            "outdated": bool(cur and lat and _norm(cur) != _norm(lat)),
            "update_id": uid if cur else None,
        }

    return [
        _item("yt-dlp",         f_ytdlp_cur.result(),   f_ytdlp_lat.result(),   "ytdlp"),
        _item("gallery-dl",     f_galldl_cur.result(),  f_galldl_lat.result(),  "galldl"),
        _item("LibreTranslate", f_lt_cur.result(),      f_lt_lat.result(),      "libretranslate"),
        _item("MetaMCP",        f_meta_cur.result(),    f_meta_lat.result(),    "metamcp"),
        _item("GitHub MCP",     f_gh_mcp_cur.result(),  f_gh_mcp_lat.result(),  "github-mcp"),
        _item("OpenClaw",       f_oc_cur.result(),      f_oc_lat.result(),      "openclaw"),
        _item("LM Studio",      f_lm_cur.result(),      f_lm_lat.result(),      None),
        _item("Docker Desktop", f_dd_cur.result(),      f_dd_lat.result(),      None),
    ]


# ── Status HTTP Server ────────────────────────────────────────────────────────

class StatusServer:
    """Serves GET /status on 127.0.0.1:8099 for mini app consumption.
    Uses cached alert monitor state — no extra health checks needed."""

    def __init__(self, bot: "Watchdog"):
        self.bot     = bot
        self._thread = threading.Thread(target=self._serve, daemon=True, name="status-server")

    def start(self):
        self._thread.start()
        print(f"[status-server] listening on 127.0.0.1:{STATUS_PORT}")

    def _serve(self):
        bot = self.bot

        class _Handler(BaseHTTPRequestHandler):
            def _send_json(self, data, status=200):
                body = json.dumps(data, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/status":
                    try:
                        cached = dict(bot.monitor._last)
                        if cached:
                            services  = {k: v for k, v in cached.items() if not k.endswith("_detail")}
                            endpoints = {k: {"ok": bool(v), "detail": cached.get(f"{k}_detail", "ok" if v else "down")}
                                         for k, v in cached.items() if not k.endswith("_detail")}
                        else:
                            svc  = get_health_snapshot(bot.cfg)
                            conn = get_connection_snapshot(bot.cfg)
                            services  = {k: (v is True) for k, v in svc.items()}
                            endpoints = {k: {"ok": ok is True, "detail": detail}
                                         for k, (ok, detail) in conn.items()}
                        disk = get_disk_snapshot()
                        lm   = get_lm_info(bot.cfg.get("lm_studio_api_key"))
                        with _contacts_lock:
                            contacts_list = _merged_contacts(bot._contacts)
                        self._send_json({
                            "services":          services,
                            "endpoints":         endpoints,
                            "disk":              [{"label": l, "used_gb": u, "total_gb": t, "pct": p}
                                                  for l, u, t, p in disk],
                            "lm_model":          lm,
                            "timestamp":         datetime.now().isoformat(),
                            "oc_dupe_conflict":  bot.monitor._oc_dupe_alerted,
                            "contacts":          contacts_list,
                        })
                    except Exception as e:
                        self._send_json({"error": str(e)}, 500)

                elif self.path == "/versions":
                    try:
                        self._send_json(get_versions_snapshot(bot.cfg))
                    except Exception as e:
                        self._send_json({"error": str(e)}, 500)

                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                if self.path == "/update":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        body   = json.loads(self.rfile.read(length)) if length else {}
                        uid    = body.get("update_id", "")
                        result = _run_update(uid, bot.cfg)
                        self._send_json({"ok": True, "result": result})
                    except Exception as e:
                        self._send_json({"ok": False, "error": str(e)}, 500)

                elif self.path == "/restart":
                    # Body: {"label": "MetaMCP" | "Reminders MCP" | "Shopping MCP" | ...}
                    # Looks up the restart map and re-runs the matching auto-restart.
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        body   = json.loads(self.rfile.read(length)) if length else {}
                        label  = str(body.get("label", "")).strip()
                        rmap   = _build_restart_map(bot.cfg)
                        spec   = rmap.get(label)
                        if not spec:
                            self._send_json({"ok": False, "error": f"unknown service: {label!r}",
                                             "available": list(rmap.keys())}, 400)
                            return
                        kind, name = spec[0], (spec[1] if len(spec) > 1 else None)
                        port       = spec[2] if len(spec) > 2 else None
                        # Run synchronously — short ops only
                        if kind == "docker":
                            ok = docker_restart(name)
                            self._send_json({"ok": ok, "label": label, "kind": kind})
                        elif kind == "proc":
                            script_dir = os.path.dirname(name) if name else ""
                            venv_py = os.path.join(script_dir, ".venv", "Scripts", "python.exe")
                            cmd = ([venv_py, "-u", name]
                                   if script_dir and os.path.exists(venv_py)
                                   else ["py", "-3", "-u", name])
                            # Kill any existing process on this port first
                            if port:
                                r = subprocess.run(["netstat", "-ano"], capture_output=True,
                                                    text=True, creationflags=_NO_WINDOW)
                                for line in r.stdout.splitlines():
                                    if f":{port} " in line and "LISTENING" in line:
                                        pid = line.split()[-1]
                                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                                       capture_output=True, creationflags=_NO_WINDOW)
                            subprocess.Popen(
                                cmd, cwd=script_dir or None,
                                creationflags=_NO_WINDOW | subprocess.DETACHED_PROCESS,
                            )
                            time.sleep(3)
                            ok = port_open(port) if port else True
                            self._send_json({"ok": ok, "label": label, "kind": kind})
                        else:
                            self._send_json({"ok": False, "error": f"restart not supported for kind={kind!r}"}, 400)
                    except Exception as e:
                        self._send_json({"ok": False, "error": str(e)}, 500)

                elif self.path == "/logs":
                    # Body: {"container": "metamcp" | "shopping-mcp" | ..., "lines": 50}
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        body   = json.loads(self.rfile.read(length)) if length else {}
                        container = str(body.get("container", "")).strip()
                        lines     = max(1, min(int(body.get("lines", 50)), 500))
                        if not container.replace("-", "").replace("_", "").isalnum():
                            self._send_json({"error": "bad container name"}, 400)
                            return
                        r = subprocess.run(
                            ["docker", "logs", "--tail", str(lines), container],
                            capture_output=True, text=True, timeout=15,
                            creationflags=_NO_WINDOW,
                        )
                        if r.returncode != 0:
                            self._send_json({"ok": False, "error": r.stderr.strip()[:400]}, 404)
                            return
                        # Merge stdout + stderr in chronological order isn't perfect via
                        # `docker logs` but combined here is good enough for diagnosis.
                        combined = (r.stderr or "") + "\n" + (r.stdout or "")
                        self._send_json({"ok": True, "container": container,
                                          "lines": lines, "log": combined})
                    except Exception as e:
                        self._send_json({"ok": False, "error": str(e)}, 500)
                elif self.path == "/contacts":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        body   = json.loads(self.rfile.read(length)) if length else {}
                        chat_id = str(body.get("chat_id", "")).strip()
                        if not chat_id:
                            self._send_json({"error": "chat_id required"}, 400)
                            return
                        first_name = str(body.get("first_name", "")).strip()
                        username   = str(body.get("username",   "")).strip()
                        with _contacts_lock:
                            existing = bot._contacts.get(chat_id, {})
                            bot._contacts[chat_id] = {
                                "chat_id":       chat_id,
                                "first_name":    first_name or existing.get("first_name", ""),
                                "username":      username   or existing.get("username",   ""),
                                "registered_at": existing.get("registered_at",
                                                              datetime.now(timezone.utc).isoformat()),
                            }
                            _save_contacts(bot._contacts)
                        print(f"[contacts] registered via POST: {chat_id}")
                        self._send_json({"ok": True, "chat_id": chat_id})
                    except Exception as e:
                        self._send_json({"error": str(e)}, 500)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *_args):
                pass

        HTTPServer(("127.0.0.1", STATUS_PORT), _Handler).serve_forever()


# ── Browser Activity Notifier (V3 Phase 1.4) ─────────────────────────────────
# Watches the Playwright MCP screenshot directory; when new files appear (the
# agent just used the browser), sends owner a Telegram message with an inline
# button that deep-links to the mini app's Browser panel. Debounced so a single
# navigation produces one notification, not one per frame.

class BrowserActivityNotifier:
    def __init__(self, bot, idle_secs: int = 90, poll_interval: int = 5):
        self.bot = bot
        self.idle_secs = idle_secs           # gap before next notification
        self.poll_interval = poll_interval   # seconds between dir scans
        self._thread = threading.Thread(target=self._run, daemon=True, name="browser-notif")
        self._stop = threading.Event()
        self._last_seen_mtime = 0.0
        self._last_notified_at = 0.0

    def start(self):
        self._thread.start()
        print(f"[browser-notif] started (idle={self.idle_secs}s, poll={self.poll_interval}s)")

    def _run(self):
        # On first start, prime _last_seen_mtime to current latest so we don't
        # fire on stale screenshots from before the watchdog started
        try:
            files = self._latest_files()
            if files:
                self._last_seen_mtime = max(os.path.getmtime(f) for f in files)
        except Exception:
            pass

        while not self._stop.is_set():
            try:
                files = self._latest_files()
                if files:
                    latest_mtime = max(os.path.getmtime(f) for f in files)
                    if latest_mtime > self._last_seen_mtime:
                        self._last_seen_mtime = latest_mtime
                        # Debounce: only notify if we haven't recently
                        if time.time() - self._last_notified_at > self.idle_secs:
                            self._notify()
                            self._last_notified_at = time.time()
            except Exception as e:
                print(f"[browser-notif] {e}")
            self._stop.wait(self.poll_interval)

    def _latest_files(self):
        if not os.path.isdir(PLAYWRIGHT_SCREENSHOT_DIR):
            return []
        try:
            return [os.path.join(PLAYWRIGHT_SCREENSHOT_DIR, f)
                    for f in os.listdir(PLAYWRIGHT_SCREENSHOT_DIR)
                    if f.startswith("page-") and f.endswith(".jpeg")]
        except Exception:
            return []

    def _notify(self):
        # The watchdog config's mini_app_url often points at a t.me deep-link,
        # but Telegram WebApp buttons need a raw https URL. Read from the
        # root metamcp-local/config.json which has the canonical https URL.
        mini_url = ""
        try:
            root_cfg_path = Path(__file__).parent.parent / "config.json"
            with open(root_cfg_path, encoding="utf-8") as f:
                mini_url = (json.load(f).get("mini_app_url") or "").strip()
        except Exception:
            mini_url = self.bot.cfg.get("mini_app_url", "")
        if not mini_url or not mini_url.startswith("https://"):
            return
        url = f"{mini_url}?panel=browser"
        kb = {"inline_keyboard": [[
            {"text": "🌐 Watch agent's browser", "web_app": {"url": url}}
        ]]}
        try:
            self.bot.send(self.bot.owner,
                          "🌐 Agent is using the browser — open the mini app to watch live.",
                          reply_markup=kb)
        except Exception as e:
            print(f"[browser-notif] send error: {e}")


# ── Telegram poller ───────────────────────────────────────────────────────────

class Watchdog:
    def __init__(self, cfg: dict):
        self.cfg    = cfg
        self.token  = cfg["bot_token"]
        self.owner  = cfg["owner_chat_id"]
        global _CB_KEY
        _CB_KEY = hmac.new(b"wdog-cb", self.token.encode(), hashlib.sha256).digest()
        self.base   = f"https://api.telegram.org/bot{self.token}"
        self.offset = 0
        self.monitor       = AlertMonitor(self)
        self.digest        = (
            DigestScheduler(self, cfg.get("digest_time", "08:00"))
            if cfg.get("digest_enabled", False) else None
        )
        self.dns           = DnsMonitor(self)
        self.github_syncer = GitHubSyncer(cfg)
        self.status_server = StatusServer(self)
        self._contacts     = _load_contacts()
        from guest_caps import GuestCapMonitor
        self.guest_caps    = GuestCapMonitor(self, interval=cfg.get("guest_cap_interval", 60))
        self.browser_notif = BrowserActivityNotifier(self)

    # ── Telegram API ──────────────────────────────────────────────────────────

    def _get_updates(self, timeout: int = 30) -> list:
        try:
            r = requests.get(
                f"{self.base}/getUpdates",
                params={
                    "offset": self.offset,
                    "timeout": timeout,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=timeout + 5,
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]
        except Exception:
            pass
        return []

    def send(self, chat_id: int, text: str, reply_markup: dict | None = None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(f"{self.base}/sendMessage", json=payload, timeout=10)
        except Exception:
            pass

    def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(f"{self.base}/editMessageText", json=payload, timeout=10)
        except Exception:
            pass

    def answer_callback(self, callback_id: str, text: str = ""):
        try:
            requests.post(
                f"{self.base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

    def set_commands(self):
        commands = [
            {"command": "status",  "description": "Health check all services"},
            {"command": "restart", "description": "Restart menu with buttons"},
            {"command": "model",   "description": "List models and switch with buttons"},
            {"command": "logs",    "description": "Last 20 OpenClaw log lines"},
            {"command": "alerts",  "description": "Show alert monitor status"},
            {"command": "dns",     "description": "DNS propagation status"},
            {"command": "help",    "description": "Show all commands"},
        ]
        try:
            requests.post(f"{self.base}/setMyCommands", json={"commands": commands}, timeout=10)
        except Exception:
            pass

    # ── Restart operations ────────────────────────────────────────────────────

    def _restart_aistack(self) -> str:
        compose_file = os.path.join(self.cfg["compose_dir"], "docker-compose.local.yml")
        result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "restart"],
            capture_output=True, text=True, timeout=120,
            creationflags=_NO_WINDOW,
        )
        return "AI Stack restarted." if result.returncode == 0 else \
               f"AI Stack restart failed:\n<code>{result.stderr[:400]}</code>"

    def _restart_openclaw(self) -> str:
        global _openclaw_last_restart
        _openclaw_last_restart = time.time()  # reset cooldown so auto-restart waits after a manual one
        svc = self.cfg["openclaw_service"]
        out = wsl(
            f"systemctl reset-failed {svc}; systemctl restart {svc} && sleep 3 && systemctl is-active {svc}",
            self.cfg, timeout=35,
        )
        ok = out.strip() == "active"
        return f"Sentinel restart: {'done' if ok else f'FAILED ({out})'}"

    def _restart_container(self, container: str) -> str:
        label = next((l for c, l in MONITORED_CONTAINERS if c == container), container)
        ok = docker_restart(container)
        return f"{label}: {'restarted.' if ok else 'restart FAILED.'}"

    # ── Stack lifecycle (run in background threads) ───────────────────────────

    def _launch_power_op(self, chat_id: int, target: str):
        fn = self._start_aistack if target == "start" else self._stop_aistack
        threading.Thread(target=fn, args=(chat_id,), daemon=True,
                         name=f"power-{target}").start()

    def _start_aistack(self, chat_id: int):
        def step(msg):
            self.send(chat_id, msg)

        try:
            # 1. Docker Desktop
            docker_up = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5,
                creationflags=_NO_WINDOW,
            ).returncode == 0
            if not docker_up:
                step("▶ Starting Docker Desktop...")
                subprocess.Popen([DOCKER_EXE], creationflags=_NO_WINDOW)
                for _ in range(18):
                    time.sleep(5)
                    if subprocess.run(
                        ["docker", "info"], capture_output=True, timeout=5,
                        creationflags=_NO_WINDOW,
                    ).returncode == 0:
                        break
                else:
                    step("❌ Docker did not start within 90s. Aborting.")
                    return
                step("✅ Docker ready")
            else:
                step("✅ Docker already running")

            # 2. Containers
            step("▶ Starting containers...")
            compose_dir = self.cfg["compose_dir"]
            for fname in self.cfg["compose_files"]:
                fpath = os.path.join(compose_dir, fname)
                subprocess.run(
                    ["docker", "compose", "-f", fpath, "up", "-d"],
                    capture_output=True, timeout=120, creationflags=_NO_WINDOW,
                )

            # 3. Wait for MetaMCP healthy
            step("▶ Waiting for MetaMCP to be healthy...")
            for _ in range(18):
                time.sleep(5)
                if docker_inspect("metamcp", "{{.State.Health.Status}}") == "healthy":
                    break
            else:
                step("⚠️ MetaMCP not healthy after 90s — continuing anyway")

            # 4. OpenClaw
            step("✅ Containers up\n▶ Starting OpenClaw...")
            svc = self.cfg["openclaw_service"]
            wsl(f"systemctl start {svc}", self.cfg)

            # 5. LM Studio
            step("✅ OpenClaw started\n▶ Starting LM Studio...")
            if not port_open(1234):
                subprocess.Popen([self.cfg["lm_studio_exe"]], creationflags=_NO_WINDOW)
                for _ in range(18):
                    time.sleep(5)
                    if port_open(1234):
                        break
                else:
                    step("⚠️ LM Studio API not ready after 90s — continuing")

            # 6. Playwright
            step("✅ LM Studio started\n▶ Starting Playwright MCP...")
            if not port_open(8932):
                subprocess.run(
                    ["schtasks", "/Run", "/TN", PLAYWRIGHT_TASK],
                    capture_output=True, timeout=10, creationflags=_NO_WINDOW,
                )
                time.sleep(8)

            # 7. Inference bridge
            step("✅ Playwright started\n▶ Starting Inference Bridge...")
            if not port_open(8095):
                subprocess.Popen(
                    ["py", "-3", "-u", self.cfg["infer_bridge"]],
                    creationflags=_NO_WINDOW | subprocess.DETACHED_PROCESS,
                )
                time.sleep(3)

            # 7b. Shopping MCP (native Python with its own venv)
            shopping_script = self.cfg.get("shopping_mcp") or r"C:\Users\azfar\sentinel-shopping\mcp_server.py"
            shopping_dir    = os.path.dirname(shopping_script)
            shopping_venv   = os.path.join(shopping_dir, ".venv", "Scripts", "python.exe")
            if not port_open(8100) and os.path.exists(shopping_script):
                step("▶ Starting Shopping MCP...")
                cmd = ([shopping_venv, "-u", shopping_script]
                       if os.path.exists(shopping_venv)
                       else ["py", "-3", "-u", shopping_script])
                subprocess.Popen(
                    cmd, cwd=shopping_dir,
                    creationflags=_NO_WINDOW | subprocess.DETACHED_PROCESS,
                )
                time.sleep(4)

            # 8. Final status
            svc_snap  = get_health_snapshot(self.cfg)
            conn_snap = get_connection_snapshot(self.cfg)
            lines = ["<b>✅ AI Stack started</b>\n\n<b>Services</b>"]
            for label, ok in svc_snap.items():
                lines.append(f"{icon(ok)} {label}")
            lines.append("\n<b>Endpoints</b>")
            for label, (ok, detail) in conn_snap.items():
                lines.append(f"{icon(ok)} {label}: {detail}")
            step("\n".join(lines))

        except Exception as e:
            step(f"❌ Start failed: {e}")

    def _stop_aistack(self, chat_id: int):
        def step(msg):
            self.send(chat_id, msg)

        try:
            # 1. LM Studio
            step("⏹ Stopping LM Studio...")
            subprocess.run(
                ["taskkill", "/IM", "LM Studio.exe", "/F"],
                capture_output=True, creationflags=_NO_WINDOW,
            )

            # 1b. Inference bridge + Shopping MCP (both native python)
            r = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                creationflags=_NO_WINDOW,
            )
            for line in r.stdout.splitlines():
                for port_str in (":8095 ", ":8100 "):
                    if port_str in line and "LISTENING" in line:
                        pid = line.split()[-1]
                        subprocess.run(
                            ["taskkill", "/PID", pid, "/F"],
                            capture_output=True, creationflags=_NO_WINDOW,
                        )

            # 2. Playwright
            step("✅ LM Studio stopped\n⏹ Stopping Playwright MCP...")
            for port in [8931, 8932]:
                r = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True,
                    creationflags=_NO_WINDOW,
                )
                for line in r.stdout.splitlines():
                    if f":{port} " in line and "LISTENING" in line:
                        pid = line.split()[-1]
                        subprocess.run(
                            ["taskkill", "/PID", pid, "/F"],
                            capture_output=True, creationflags=_NO_WINDOW,
                        )

            # 3. OpenClaw
            step("✅ Playwright stopped\n⏹ Stopping OpenClaw...")
            svc = self.cfg["openclaw_service"]
            wsl(f"systemctl stop {svc}", self.cfg)

            # 4. Containers
            step("✅ OpenClaw stopped\n⏹ Stopping containers...")
            compose_dir = self.cfg["compose_dir"]
            for fname in self.cfg["compose_files"]:
                fpath = os.path.join(compose_dir, fname)
                subprocess.run(
                    ["docker", "compose", "-f", fpath, "down"],
                    capture_output=True, timeout=120, creationflags=_NO_WINDOW,
                )

            step(
                "✅ AI Stack stopped.\n\n"
                "<i>Docker Desktop is still running — quit it from the system tray "
                "if you want a full shutdown.</i>"
            )

        except Exception as e:
            step(f"❌ Stop failed: {e}")

    # ── Callback handlers ─────────────────────────────────────────────────────

    def _handle_restart_callback(self, callback_id: str, chat_id: int, message_id: int, action: str):
        if action == "menu":
            self.answer_callback(callback_id)
            self.edit_message(chat_id, message_id,
                "<b>What would you like to restart?</b>", _restart_main_menu())

        elif action == "services":
            self.answer_callback(callback_id)
            self.edit_message(chat_id, message_id,
                "<b>Choose a container to restart:</b>", _restart_services_menu())

        elif action == "aistack":
            self.answer_callback(callback_id, "Restarting AI Stack...")
            self.edit_message(chat_id, message_id, "Restarting AI Stack — please wait...")
            result = self._restart_aistack()
            self.edit_message(chat_id, message_id,
                f"{result}\n\n<b>What would you like to restart?</b>", _restart_main_menu())

        elif action == "openclaw":
            self.answer_callback(callback_id, "Restarting Sentinel...")
            self.edit_message(chat_id, message_id, "Restarting Sentinel — please wait...")
            result = self._restart_openclaw()
            self.edit_message(chat_id, message_id,
                f"{result}\n\n<b>What would you like to restart?</b>", _restart_main_menu())

        elif action == "smdl":
            self.answer_callback(callback_id, "Restarting smdl...")
            self.edit_message(chat_id, message_id, "Restarting smdl — please wait...")
            result = self._restart_container("smdl")
            self.edit_message(chat_id, message_id,
                f"{result}\n\n<b>What would you like to restart?</b>", _restart_main_menu())

        elif action.startswith("svc:"):
            container = action[4:]
            _allowed = {c for c, _ in MONITORED_CONTAINERS}
            if container not in _allowed:
                self.answer_callback(callback_id, "Unknown container")
                return
            label = next((l for c, l in MONITORED_CONTAINERS if c == container), container)
            self.answer_callback(callback_id, f"Restarting {label}...")
            self.edit_message(chat_id, message_id, f"Restarting {label} — please wait...")
            result = self._restart_container(container)
            self.edit_message(chat_id, message_id,
                f"{result}\n\n<b>What would you like to restart?</b>", _restart_main_menu())

    def _handle_model_callback(self, callback_id: str, chat_id: int, message_id: int, action: str):
        oc, err = _read_openclaw(self.cfg)
        if err:
            self.answer_callback(callback_id, "Error reading config")
            return

        primary = oc.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")

        if action == "menu":
            self.answer_callback(callback_id)
            self.edit_message(chat_id, message_id,
                "<b>Select provider:</b>", _model_provider_menu(primary))

        elif action.startswith("p:"):
            provider = action[2:]
            label = "LM Studio" if provider == "lmstudio" else "OpenRouter (free)"
            self.answer_callback(callback_id)
            self.edit_message(chat_id, message_id,
                f"<b>{label} — select model:</b>", _model_list_menu(oc, provider))

        elif action.startswith("set:"):
            model_id = action[4:]
            self.answer_callback(callback_id, f"Switching to {model_id.split('/')[-1]}...")
            result = _do_switch_model(model_id, self.cfg)
            # Re-read after write so ✓ reflects new primary
            oc2, _ = _read_openclaw(self.cfg)
            if oc2:
                primary2  = oc2.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
                provider  = model_id.split("/")[0]
                label     = "LM Studio" if provider == "lmstudio" else "OpenRouter (free)"
                self.edit_message(chat_id, message_id,
                    f"<b>{label} — select model:</b>", _model_list_menu(oc2, provider))
            self.send(chat_id, result)

    def _handle_power_callback(self, callback_id: str, chat_id: int, message_id: int, action: str):
        if action == "confirm_start":
            self.answer_callback(callback_id)
            self.edit_message(chat_id, message_id,
                "▶ <b>Start the full AI stack?</b>\n"
                "This will launch Docker containers, OpenClaw, LM Studio, and Playwright.",
                _power_confirm_menu("start", "Yes, start it"))

        elif action == "confirm_stop":
            self.answer_callback(callback_id)
            self.edit_message(chat_id, message_id,
                "⏹ <b>Stop the full AI stack?</b>\n"
                "This will stop OpenClaw, all containers, LM Studio, and Playwright.\n"
                "<i>Docker Desktop will stay running.</i>",
                _power_confirm_menu("stop", "Yes, stop it"))

        elif action == "do_start":
            self.answer_callback(callback_id, "Starting stack...")
            self.edit_message(chat_id, message_id,
                "▶ Starting AI Stack — progress updates below...")
            self._launch_power_op(chat_id, "start")

        elif action == "do_stop":
            self.answer_callback(callback_id, "Stopping stack...")
            self.edit_message(chat_id, message_id,
                "⏹ Stopping AI Stack — progress updates below...")
            self._launch_power_op(chat_id, "stop")

    # ── Message / callback dispatch ───────────────────────────────────────────

    def handle_message(self, msg: dict):
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "").strip()

        if chat_id != self.owner:
            if text == "/start":
                first_name = msg.get("from", {}).get("first_name", "")
                username   = msg.get("from", {}).get("username", "")
                key        = str(chat_id)
                with _contacts_lock:
                    self._contacts[key] = {
                        "chat_id":      key,
                        "first_name":   first_name,
                        "username":     username,
                        "registered_at": datetime.now(timezone.utc).isoformat(),
                    }
                    _save_contacts(self._contacts)
                display = first_name or username or key
                self.send(chat_id,
                    f"✅ <b>{display}</b>, you're registered to receive reminders from this bot.\n"
                    f"Your chat ID: <code>{key}</code>")
            return

        if not text.startswith("/"):
            return

        parts = text.split()
        raw_cmd = parts[0].lstrip("/").lower().split("@")[0]
        args = parts[1:]

        if raw_cmd == "alerts":
            self.send(chat_id, self.monitor.status_text())
            return

        if raw_cmd == "dns":
            self.send(chat_id, self.dns.current_status())
            return

        if raw_cmd == "sync":
            if not self.github_syncer.repo:
                self.send(chat_id, "GitHub sync not configured (set github_repo + github_pat in config.json)")
            else:
                self.send(chat_id, "⏳ GitHub sync triggered — check logs for results.")
                self.github_syncer.trigger()
            return

        handler = COMMANDS.get(raw_cmd)
        if handler:
            try:
                result = handler(args, self.cfg)
            except Exception as e:
                result = f"Error running /{raw_cmd}: {e}"
        else:
            result = f"Unknown command: /{raw_cmd}\n\n" + cmd_help([], self.cfg)

        if isinstance(result, tuple):
            self.send(chat_id, result[0], reply_markup=result[1])
        else:
            self.send(chat_id, result)

    def handle_callback(self, cb: dict):
        chat_id = cb.get("from", {}).get("id")
        if chat_id != self.owner:
            return

        callback_id = cb["id"]
        message_id  = cb.get("message", {}).get("message_id")

        data = _cb_verify(cb.get("data", ""))
        if data is None:
            self.answer_callback(callback_id, "Invalid request")
            return

        if data.startswith(CALLBACK_RESTART):
            self._handle_restart_callback(callback_id, chat_id, message_id,
                                          data[len(CALLBACK_RESTART):])

        elif data.startswith(CALLBACK_MODEL):
            self._handle_model_callback(callback_id, chat_id, message_id,
                                        data[len(CALLBACK_MODEL):])

        elif data.startswith(CALLBACK_POWER):
            self._handle_power_callback(callback_id, chat_id, message_id,
                                        data[len(CALLBACK_POWER):])

        elif data.startswith(CALLBACK_LOGS):
            self._handle_logs_callback(callback_id, chat_id,
                                       data[len(CALLBACK_LOGS):])

        else:
            self.answer_callback(callback_id)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _handle_logs_callback(self, callback_id: str, chat_id: int, action: str):
        if action.startswith("openclaw:"):
            n = min(int(action.split(":")[1]), 50)
            self.answer_callback(callback_id, "Fetching OpenClaw logs...")
            out = wsl(
                f"journalctl -u {self.cfg['openclaw_service']} -n {n} --no-pager -o short",
                self.cfg, timeout=15,
            )
            if len(out) > 3800:
                out = "[truncated]\n..." + out[-3800:]
            self.send(chat_id, f"<b>OpenClaw (last {n} lines)</b>\n<pre>{out}</pre>")

        elif action.startswith("docker:"):
            container = action[len("docker:"):]
            _allowed = {c for c, _ in MONITORED_CONTAINERS}
            if container not in _allowed:
                self.answer_callback(callback_id, "Unknown container")
                return
            label = next((l for c, l in MONITORED_CONTAINERS if c == container), container)
            self.answer_callback(callback_id, f"Fetching {label} logs...")
            result = subprocess.run(
                ["docker", "logs", container, "--tail", "30"],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
            out = (result.stdout + result.stderr).strip()
            if len(out) > 3800:
                out = "[truncated]\n..." + out[-3800:]
            self.send(chat_id, f"<b>{label} (last 30 lines)</b>\n<pre>{out or '(empty)'}</pre>")

        else:
            self.answer_callback(callback_id)

    def run(self):
        self.set_commands()
        self.monitor.start()
        if self.digest:
            self.digest.start()
        self.dns.start()
        self.github_syncer.start()
        self.status_server.start()
        self.guest_caps.start()
        self.browser_notif.start()
        print(f"Watchdog polling (owner: {self.owner})")
        while True:
            updates = self._get_updates()
            for update in updates:
                self.offset = update["update_id"] + 1
                if "message" in update:
                    self.handle_message(update["message"])
                elif "callback_query" in update:
                    self.handle_callback(update["callback_query"])


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = load_config()
    if cfg["bot_token"] == "REPLACE_WITH_BOTFATHER_TOKEN":
        print("ERROR: set bot_token in config.json before running")
        sys.exit(1)

    bot = Watchdog(cfg)
    while True:
        try:
            bot.run()
        except KeyboardInterrupt:
            print("Stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"Crash: {e} — restarting in 15s")
            time.sleep(15)
