"""Sentinel VPN dashboard — unified status surface for all 4 VPN routes.

Routes shown:
  1. Cloudflare Tunnel — public hostnames + backend health probes
  2. Headscale         — registered users + tailnet nodes + last-seen
  3. AmneziaWG         — hub peers + last handshake + transfer bytes
  4. ARK direct        — UDP 7777 + TCP 27015 listening check

Tonight's scope = read-only. The page polls /api/* every 30s.

Auth: bound to host loopback in compose (`127.0.0.1:8097`) so it's
only reachable from the LAN OR the Tailscale tailnet. Phase 2 will add
Telegram initData auth + CF Tunnel route at vpn.your-domain.example.com.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import auth_v2
import users_db


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vpn-dashboard")


# ── APK cookie auth (shared across all *.your-domain.example.com subdomains) ─────────
# Same scheme is implemented in sentinel-miniapp-v2/bridge.py and
# sentinel-smdl/app/miniapp.py so one /auth/setup hit authorises all three.
OWNER_AUTH_TOKEN = os.environ.get("OWNER_AUTH_TOKEN", "")
COOKIE_NAME      = "sentinel_apk_session"
COOKIE_DOMAIN    = ".your-domain.example.com"
COOKIE_TTL_SEC   = 90 * 24 * 3600  # 90 days

# ── License Registry (watchdog v2 :8200) ────────────────────────────────────
# The /admin/licenses console reads + revokes through the host's registry.
# Metadata-only mirror (no secrets ever cross this boundary). Auth is a v2
# service token in the X-Sentinel-Service-Token header. Both env vars are
# optional — absence degrades the console to a "registry unavailable" notice.
LICENSE_REGISTRY_URL   = os.environ.get("LICENSE_REGISTRY_URL", "http://host.docker.internal:8200").rstrip("/")
LICENSE_REGISTRY_TOKEN = os.environ.get("LICENSE_REGISTRY_TOKEN", "")

# ── Notifications (shared feed served by the agent dashboard bridge :8098) ───
# Both hubs read ONE store. This launcher proxies read+mark server-side to the
# host bridge, forwarding the owner's domain-wide sentinel_apk_session cookie so
# the bridge can authenticate it (no extra secret needed — same OWNER_AUTH_TOKEN).
NOTIFY_BRIDGE_URL = os.environ.get("NOTIFY_BRIDGE_URL", "http://host.docker.internal:8098").rstrip("/")


def _issue_cookie() -> str:
    """HMAC-signed `<ts>.<nonce>.<sig>` payload — signature key is OWNER_AUTH_TOKEN.
    This is the v1 (owner) cookie. v2 (scoped beta) cookies are minted via
    auth_v2.issue_v2_cookie in /auth/redeem."""
    ts    = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    body  = f"{ts}.{nonce}"
    sig   = hmac.new(OWNER_AUTH_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _get_payload(request: Request) -> dict | None:
    """Parse the session cookie via auth_v2 (handles both v1 and v2).
    Returns None on any failure (missing, malformed, bad-sig, expired,
    revoked). v1 owner cookies authenticate exactly as before."""
    raw = request.cookies.get(COOKIE_NAME, "")
    if not raw or not OWNER_AUTH_TOKEN:
        return None
    try:
        payload = auth_v2.parse_session_cookie(raw, OWNER_AUTH_TOKEN)
    except HTTPException:
        return None
    if payload.get("expired"):
        return None
    jti = payload.get("jti") or ""
    if jti and users_db.is_revoked(jti):
        return None
    return payload


def _is_authed(request: Request) -> bool:
    return _get_payload(request) is not None


def _is_owner(request: Request) -> bool:
    p = _get_payload(request)
    return p is not None and p.get("user_id") == "owner"


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() \
        or (request.client.host if request.client else "")


# ── Cloudflare Tunnel hostnames ─────────────────────────────────────────────
# Hardcoded from the Zero Trust dashboard (cheaper + simpler than CF API).
# Update when you add or rename a route.
CF_HOSTNAMES: list[dict[str, str]] = [
    {"hostname": "your-domain.example.com",       "backend": "http://localhost:8098", "label": "Sentinel Bridge"},
    {"hostname": "headscale.your-domain.example.com",      "backend": "http://localhost:8081", "label": "Headscale"},
    {"hostname": "media.your-domain.example.com",          "backend": "http://localhost:8096", "label": "SMDL Media"},
    {"hostname": "vault.your-domain.example.com",          "backend": "http://localhost:8085", "label": "Vaultwarden"},
    {"hostname": "firefly.your-domain.example.com",        "backend": "http://localhost:8180", "label": "Firefly III"},
    {"hostname": "sentinelfinance.your-domain.example.com","backend": "http://localhost:8086", "label": "Sentinel Finance"},
    {"hostname": "sentinelgaming.your-domain.example.com", "backend": "http://localhost:8084", "label": "Sentinel Gaming"},
    {"hostname": "suite.your-domain.example.com",          "backend": "http://localhost:8083", "label": "Suite launcher (this app · / = launcher, /network = Sentinel Network)"},
    {"hostname": "watchdog.your-domain.example.com",       "backend": "http://localhost:8200", "label": "Watchdog v2 (ops console)"},
]

# ARK port forwards (per memory: ViewQwest static IP 137.59.185.35).
ARK_FORWARDS = [
    {"proto": "udp", "port": 7777,  "label": "ARK game"},
    {"proto": "tcp", "port": 27015, "label": "ARK Steam query"},
]


# ── Cache (so the page doesn't hammer subprocesses on every refresh) ─────────
_cache: dict[str, dict] = {}
CACHE_TTL_SEC = 20


def _cache_get(key: str):
    e = _cache.get(key)
    if e and time.time() - e["ts"] < CACHE_TTL_SEC:
        return e["v"]
    return None


def _cache_set(key: str, value):
    _cache[key] = {"v": value, "ts": time.time()}


# ── Probe helpers ────────────────────────────────────────────────────────────


async def _http_probe(url: str, timeout: float = 4.0) -> dict:
    """HEAD a URL, fall back to GET. Auth-walled endpoints (401/403) are still
    considered 'up' — the *gateway* is alive even if the user isn't auth'd."""
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.head(url)
            if r.status_code == 405:  # some backends reject HEAD
                r = await c.get(url)
        ms = int((time.time() - t0) * 1000)
        up = r.status_code < 500
        return {"ok": up, "code": r.status_code, "latency_ms": ms,
                "note": "auth gate" if r.status_code in (401, 403) else ""}
    except httpx.TimeoutException:
        return {"ok": False, "code": 0, "latency_ms": int(timeout * 1000), "note": "timeout"}
    except Exception as e:
        return {"ok": False, "code": 0, "latency_ms": 0, "note": str(e)[:80]}


def _docker_exec(container: str, cmd: list[str], timeout: int = 8) -> tuple[int, str, str]:
    """Run `docker exec <container> <cmd>` from the host. Returns (rc, stdout, stderr)."""
    try:
        full = ["docker", "exec", container] + cmd
        proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -2, "", str(e)[:200]


def _check_port(host: str, port: int, proto: str = "tcp", timeout: float = 1.0) -> bool:
    """Best-effort port probe. TCP = connect; UDP = can't reliably probe, so
    we just confirm something is bound locally via netstat-style check."""
    import socket
    if proto == "tcp":
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False
    if proto == "udp":
        # UDP is connectionless — sending a probe doesn't confirm a listener.
        # We just check whether anything is bound to this port from the host.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((host, port))
            s.close()
            return False  # bind succeeded → nothing was using it
        except OSError:
            return True   # bind failed → someone is using it
    return False


# ── Route probes ─────────────────────────────────────────────────────────────


async def probe_cloudflare() -> dict:
    """Hit each public hostname externally + report status. We probe the
    public URL (not the local backend) so this measures the full
    user → CF edge → tunnel → service path."""
    cached = _cache_get("cf")
    if cached:
        return cached
    tasks = [_http_probe(f"https://{h['hostname']}") for h in CF_HOSTNAMES]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    routes = []
    up_count = 0
    for h, r in zip(CF_HOSTNAMES, results):
        routes.append({**h, **r})
        if r["ok"]:
            up_count += 1
    out = {
        "label":      "Cloudflare Tunnel (HTTPS)",
        "summary":    f"{up_count}/{len(CF_HOSTNAMES)} routes reachable",
        "all_up":     up_count == len(CF_HOSTNAMES),
        "routes":     routes,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_set("cf", out)
    return out


def probe_headscale() -> dict:
    """`headscale users list` + `headscale nodes list` via docker exec."""
    cached = _cache_get("hs")
    if cached:
        return cached
    rc_u, out_u, err_u = _docker_exec("headscale", ["headscale", "users", "list", "--output", "json"])
    rc_n, out_n, err_n = _docker_exec("headscale", ["headscale", "nodes", "list", "--output", "json"])

    users: list = []
    nodes: list = []
    err = ""
    if rc_u == 0 and out_u.strip():
        try:
            parsed = json.loads(out_u)
            users = parsed if isinstance(parsed, list) else []
        except Exception as e:
            err = f"users parse: {e!s:.80}"
    elif rc_u != 0:
        err = (err_u or "")[:200]

    if rc_n == 0 and out_n.strip():
        try:
            parsed = json.loads(out_n)
            nodes = parsed if isinstance(parsed, list) else []
        except Exception as e:
            err = (err + " · nodes parse: " + str(e)[:80]).strip(" ·")
    elif rc_n != 0:
        err = (err + " · " + (err_n or "")[:200]).strip(" ·")

    # Normalise node fields — Headscale's JSON has different shapes across versions.
    norm_nodes = []
    online_count = 0
    for n in nodes:
        is_online = bool(n.get("online", False))
        if is_online:
            online_count += 1
        ips = n.get("ip_addresses") or n.get("ipAddresses") or []
        ipv4 = next((ip for ip in ips if ":" not in (ip or "")), (ips[0] if ips else None))
        norm_nodes.append({
            "id":          str(n.get("id") or ""),
            "name":        n.get("name", "?"),
            "given_name":  n.get("given_name") or n.get("givenName") or "",
            "ipv4":        ipv4,
            "user":        (n.get("user") or {}).get("name") if isinstance(n.get("user"), dict) else n.get("user", ""),
            "online":      is_online,
            # Headscale emits timestamps as protobuf {seconds,nanos} objects on
            # `nodes list --output json`; normalise to ISO strings so the client
            # can slice/format them (a raw object broke `last_seen.slice` for any
            # offline node). _ts_to_iso is defined below — resolved at call time.
            "last_seen":   _ts_to_iso(n.get("last_seen") or n.get("lastSeen")),
            "created_at":  _ts_to_iso(n.get("created_at") or n.get("createdAt")),
            "expiry":      _ts_to_iso(n.get("expiry")),
            "expiry_unix": _ts_to_unix(n.get("expiry")),
        })

    out = {
        "label":      "Headscale tailnet",
        "summary":    f"{len(users)} user(s) · {len(norm_nodes)} node(s) · {online_count} online",
        "users":      [{"id": u.get("id"), "name": u.get("name") or u.get("display_name")} for u in users],
        "nodes":      norm_nodes,
        "node_count": len(norm_nodes),
        "online":     online_count,
        "err":        err or None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_set("hs", out)
    return out


async def probe_amneziawg() -> dict:
    """Read-only AmneziaWG status via the host watchdog API. The server is
    WSL-native (Docker Desktop drops inbound UDP — ADR NET-008) and the
    containerized dashboard can't reach WSL directly, so the host (:8200, full
    WSL access) runs `awg show` and we proxy it for a read-only view here.
    Management stays in the desktop app."""
    cached = _cache_get("awg")
    if cached:
        return cached
    code, body = await _registry_call("GET", "/api/v2/net/amneziawg", timeout=12.0)
    if code == 200 and isinstance(body, dict) and "peers" in body:
        result = dict(body)
        result.setdefault("label", "AmneziaWG (friend hub-spoke)")
    else:
        result = {
            "label": "AmneziaWG (friend hub-spoke)",
            "summary": "status unavailable",
            "iface": {}, "peers": [], "err": None,
            "note": "Live read-only status needs the host watchdog API (:8200). "
                    "Manage peers in the Sentinel Network desktop app → AmneziaWG.",
        }
    result.setdefault("checked_at", datetime.now(timezone.utc).isoformat())
    _cache_set("awg", result)
    return result


def probe_ark() -> dict:
    """ARK is direct port-forward (no container in this stack today). We
    check whether the host has anything listening locally — that's the
    proxy for 'forwarded service is up'."""
    forwards = []
    up_count = 0
    for fw in ARK_FORWARDS:
        # localhost probe — confirms a process is bound, not that the public
        # path works. (Public-path test needs an external client.)
        up = _check_port("127.0.0.1", fw["port"], fw["proto"])
        forwards.append({**fw, "ok": up})
        if up:
            up_count += 1
    return {
        "label":      "ARK direct port forward",
        "summary":    f"{up_count}/{len(ARK_FORWARDS)} ports listening locally"
                      + (" (server not running)" if up_count == 0 else ""),
        "forwards":   forwards,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "note":       "Local-listen check only. To verify public reachability, ask a friend to connect via Steam.",
    }


# ── FastAPI app ──────────────────────────────────────────────────────────────


SCOPES_YAML_PATH = Path(os.environ.get("SENTINEL_SCOPES_YAML", "/data/scopes.yaml"))


def _load_scopes_yaml() -> dict:
    """Read the scope catalogue. Returns {} on read/parse error so the admin
    UI degrades gracefully (empty scope picker rather than a 500)."""
    try:
        with SCOPES_YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        scopes = data.get("scopes") or {}
        if not isinstance(scopes, dict):
            return {}
        return scopes
    except Exception as e:
        logger.warning(f"scopes.yaml unreadable: {e}")
        return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Sentinel Network/Suite dashboard starting on :8097 (SURFACE={os.environ.get('SURFACE', 'suite')})")
    try:
        users_db.init()
        logger.info(f"auth_v2 users_db ready at {users_db.DB_PATH}")
    except Exception as e:
        logger.error(f"users_db.init failed: {e}")
    # Daily expiry/cert alerter — only on the dedicated Network surface so the
    # two dashboard containers don't double-notify.
    alert_task = None
    if os.environ.get("SURFACE", "suite").strip().lower() == "network":
        alert_task = asyncio.create_task(_expiry_alert_loop())
        logger.info("expiry/cert alert loop started (SURFACE=network)")
    yield
    if alert_task:
        alert_task.cancel()
    logger.info("VPN dashboard shutting down")


# SURFACE selects what the bare `/` serves. The default "suite" instance is
# the Suite launcher (4 tiles); a dedicated "network" instance (own port +
# network.your-domain.example.com) serves the Network dashboard at its root instead,
# decoupling Sentinel Network from the suite launcher.
SURFACE = os.environ.get("SURFACE", "suite").strip().lower()

app = FastAPI(title="Sentinel Network Dashboard", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "service": "sentinel-vpn-dashboard"}


@app.get("/api/cf")
async def api_cf():
    return await probe_cloudflare()


@app.get("/api/headscale")
async def api_hs():
    return JSONResponse(probe_headscale())


@app.get("/api/amneziawg")
async def api_awg():
    return JSONResponse(await probe_amneziawg())


@app.get("/api/ark")
async def api_ark():
    return JSONResponse(probe_ark())


@app.get("/api/all")
async def api_all():
    cf = await probe_cloudflare()
    return {
        "cf":        cf,
        "headscale": probe_headscale(),
        "amneziawg": await probe_amneziawg(),
        "ark":       probe_ark(),
    }


# ── Headscale control plane (owner-only writes) ──────────────────────────────
# First mutating route into the network backend. Mirrors the desktop Tauri
# GUI's `headscale_preauthkey` action (docker exec headscale headscale …),
# gated to the owner since this surface is internet-reachable.

_HS_USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_HS_EXP_RE = re.compile(r"^[0-9]{1,4}[smhd]$")
_HS_TAG_RE = re.compile(r"^tag:[a-z0-9][a-z0-9-]{0,62}$")


def _hs_user_id(name: str) -> int | None:
    """Resolve a headscale username to its numeric user ID. headscale 0.26+
    requires the numeric ID for `preauthkeys create --user`, not the name."""
    rc, out, _ = _docker_exec("headscale", ["headscale", "users", "list", "--output", "json"])
    if rc != 0 or not out.strip():
        return None
    try:
        for u in json.loads(out):
            if (u.get("name") or "") == name:
                return int(u.get("id"))
    except Exception:
        return None
    return None


@app.post("/api/headscale/preauthkey")
async def api_hs_preauthkey(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = str(body.get("user", "")).strip()
    if not _HS_USER_RE.fullmatch(user):
        return JSONResponse({"error": "bad_user"}, status_code=400)
    expiration = str(body.get("expiration", "1h")).strip() or "1h"
    if not _HS_EXP_RE.fullmatch(expiration):
        return JSONResponse({"error": "bad_expiration"}, status_code=400)
    raw_tags = body.get("tags") or []
    if not isinstance(raw_tags, list):
        return JSONResponse({"error": "bad_tags"}, status_code=400)
    tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    if any(not _HS_TAG_RE.fullmatch(t) for t in tags):
        return JSONResponse({"error": "bad_tags"}, status_code=400)
    uid = _hs_user_id(user)
    if uid is None:
        return JSONResponse({"error": "unknown_user", "detail": user}, status_code=400)
    cmd = ["headscale", "preauthkeys", "create", "--user", str(uid),
           "--expiration", expiration, "--output", "json"]
    if bool(body.get("reusable")):
        cmd.append("--reusable")
    if bool(body.get("ephemeral")):
        cmd.append("--ephemeral")
    if tags:
        cmd += ["--tags", ",".join(tags)]
    rc, out, err = _docker_exec("headscale", cmd)
    if rc != 0:
        return JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                            status_code=502)
    key = ""
    try:
        parsed = json.loads(out)
        key = parsed.get("key") if isinstance(parsed, dict) else ""
    except Exception:
        key = out.strip().splitlines()[-1] if out.strip() else ""
    if not key:
        return JSONResponse({"error": "no_key", "detail": (out or "")[:300]}, status_code=502)
    _cache.pop("hs", None)
    payload = _get_payload(request) or {}
    try:
        users_db.log_event("headscale.preauthkey", user_id=payload.get("user_id"),
                           jti=payload.get("jti") or None, ip=_client_ip(request),
                           payload={"hs_user": user, "expiration": expiration,
                                    "reusable": bool(body.get("reusable")),
                                    "ephemeral": bool(body.get("ephemeral")),
                                    "tags": tags})
    except Exception:
        pass
    return JSONResponse({"ok": True, "key": key, "user": user, "expiration": expiration})


# ── Headscale node lifecycle + routes (owner-only) ───────────────────────────
_HS_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,63}$")
# headscale < 0.26: "nodekey:<hex>"; 0.26+: short base64url key e.g. "j2Ci7bW-MGNAPkYSb2aOrAqq"
_HS_REGKEY_RE = re.compile(r"^((nodekey|mkey|node):[a-f0-9]{16,128}|[A-Za-z0-9_-]{8,64})$")
_HS_CIDR_RE = re.compile(r"^[0-9a-fA-F:.]{2,}/[0-9]{1,3}$")
_HS_PAKEY_RE = re.compile(r"^[A-Za-z0-9]{8,256}$")


def _hs_node_id(node_id: str) -> str | None:
    return node_id if re.fullmatch(r"[0-9]{1,9}", node_id or "") else None


def _hs_run(request: Request, cmd: list[str], event: str, log_payload: dict):
    """Run a mutating headscale command, bust the cache, audit-log it.
    Returns (stdout, None) on success or (None, JSONResponse) on failure."""
    rc, out, err = _docker_exec("headscale", cmd)
    if rc != 0:
        return None, JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                                  status_code=502)
    _cache.pop("hs", None)
    p = _get_payload(request) or {}
    try:
        users_db.log_event(event, user_id=p.get("user_id"), jti=p.get("jti") or None,
                           ip=_client_ip(request), payload=log_payload)
    except Exception:
        pass
    return out, None


@app.post("/api/headscale/node/register")
async def api_hs_node_register(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = str(body.get("user", "")).strip()
    key = str(body.get("key", "")).strip()
    if not _HS_USER_RE.fullmatch(user):
        return JSONResponse({"error": "bad_user"}, status_code=400)
    if not _HS_REGKEY_RE.fullmatch(key):
        return JSONResponse({"error": "bad_key"}, status_code=400)
    # `nodes register` takes the user NAME (string), unlike preauthkeys (ID).
    _, errresp = _hs_run(request, ["headscale", "nodes", "register", "--user", user,
                                   "--key", key, "--output", "json"],
                         "headscale.node.register", {"hs_user": user})
    return errresp or JSONResponse({"ok": True})


@app.post("/api/headscale/node/{node_id}/rename")
async def api_hs_node_rename(node_id: str, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    nid = _hs_node_id(node_id)
    if nid is None:
        return JSONResponse({"error": "bad_node_id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()
    if not _HS_NODE_NAME_RE.fullmatch(name):
        return JSONResponse({"error": "bad_name"}, status_code=400)
    _, errresp = _hs_run(request, ["headscale", "nodes", "rename", "-i", nid, name],
                         "headscale.node.rename", {"node_id": nid, "name": name})
    return errresp or JSONResponse({"ok": True})


@app.post("/api/headscale/node/{node_id}/expire")
async def api_hs_node_expire(node_id: str, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    nid = _hs_node_id(node_id)
    if nid is None:
        return JSONResponse({"error": "bad_node_id"}, status_code=400)
    _, errresp = _hs_run(request, ["headscale", "nodes", "expire", "-i", nid, "--force"],
                         "headscale.node.expire", {"node_id": nid})
    return errresp or JSONResponse({"ok": True})


@app.post("/api/headscale/node/{node_id}/delete")
async def api_hs_node_delete(node_id: str, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    nid = _hs_node_id(node_id)
    if nid is None:
        return JSONResponse({"error": "bad_node_id"}, status_code=400)
    _, errresp = _hs_run(request, ["headscale", "nodes", "delete", "-i", nid, "--force"],
                         "headscale.node.delete", {"node_id": nid})
    return errresp or JSONResponse({"ok": True})


@app.get("/api/headscale/node/{node_id}/routes")
async def api_hs_node_routes_get(node_id: str, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    nid = _hs_node_id(node_id)
    if nid is None:
        return JSONResponse({"error": "bad_node_id"}, status_code=400)
    rc, out, err = _docker_exec("headscale", ["headscale", "nodes", "list-routes",
                                              "-i", nid, "--output", "json"])
    if rc != 0:
        return JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                            status_code=502)
    try:
        return JSONResponse(json.loads(out) if out.strip() else [])
    except Exception:
        return JSONResponse({"error": "parse", "detail": (out or "")[:300]}, status_code=502)


@app.post("/api/headscale/node/{node_id}/routes")
async def api_hs_node_routes_set(node_id: str, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    nid = _hs_node_id(node_id)
    if nid is None:
        return JSONResponse({"error": "bad_node_id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = body.get("routes") or []
    if not isinstance(raw, list):
        return JSONResponse({"error": "bad_routes"}, status_code=400)
    routes = [str(r).strip() for r in raw if str(r).strip()]
    if any(not _HS_CIDR_RE.fullmatch(r) for r in routes):
        return JSONResponse({"error": "bad_routes"}, status_code=400)
    # Empty string clears all approved routes (per headscale CLI semantics).
    _, errresp = _hs_run(request, ["headscale", "nodes", "approve-routes", "-i", nid,
                                   "--force", "-r", ",".join(routes)],
                         "headscale.node.routes", {"node_id": nid, "routes": routes})
    return errresp or JSONResponse({"ok": True, "routes": routes})


@app.get("/api/headscale/preauthkeys")
async def api_hs_preauthkeys_list(request: Request, user: str = ""):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    user = (user or "").strip()
    if not _HS_USER_RE.fullmatch(user):
        return JSONResponse({"error": "bad_user"}, status_code=400)
    # headscale 0.28 DROPPED `--user` from `preauthkeys list` (it's global now).
    # List all keys, then filter to the requested user by the embedded user.name.
    rc, out, err = _docker_exec("headscale", ["headscale", "preauthkeys", "list",
                                              "--output", "json"])
    if rc != 0:
        return JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                            status_code=502)
    try:
        rows = json.loads(out) if out.strip() else []
    except Exception:
        return JSONResponse({"error": "parse", "detail": (out or "")[:300]}, status_code=502)
    keys = [k for k in rows if ((k.get("user") or {}).get("name") or "") == user]
    return JSONResponse(keys)


@app.post("/api/headscale/preauthkey/expire")
async def api_hs_preauthkey_expire(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = str(body.get("user", "")).strip()
    # headscale 0.28 `preauthkeys expire` takes `-i/--id <authkey-id>`, not
    # `--user <key>`. The frontend now sends the numeric authkey id.
    raw_id = str(body.get("id", body.get("key", ""))).strip()
    if not _HS_USER_RE.fullmatch(user):
        return JSONResponse({"error": "bad_user"}, status_code=400)
    if not raw_id.isdigit():
        return JSONResponse({"error": "bad_id"}, status_code=400)
    _, errresp = _hs_run(request, ["headscale", "preauthkeys", "expire", "--id", raw_id],
                         "headscale.preauthkey.expire", {"hs_user": user, "authkey_id": raw_id})
    return errresp or JSONResponse({"ok": True})


@app.get("/api/headscale/acl")
async def api_hs_acl(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    rc, out, err = _docker_exec("headscale", ["headscale", "policy", "get"])
    if rc != 0:
        return JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                            status_code=502)
    return JSONResponse({"policy": out})


# ═══════════════════════════════════════════════════════════════════════════
#  Network dashboard — Console / Operations / Reliability feature backend
#  (2026-06-04). All mutating + sensitive routes are owner-only. Sensitive
#  config writes are look-before-overwrite: validate, snapshot, then apply.
# ═══════════════════════════════════════════════════════════════════════════

# Shared headscale config bind mount (compose: ./headscale-config -> here AND
# -> /etc/headscale in the headscale container). Lets us read/write acl.json +
# config.yaml and stage temp files the headscale binary can see at the same path.
HS_CONFIG_DIR  = Path(os.environ.get("HS_CONFIG_DIR", "/headscale-config"))
HS_ACL_PATH    = HS_CONFIG_DIR / "acl.json"
HS_CONFIG_YAML = HS_CONFIG_DIR / "config.yaml"
BACKUP_DIR     = Path(os.environ.get("HS_BACKUP_DIR", "/data/backups"))
WOL_DEVICES_PATH = Path("/data/wol_devices.json")
HS_PUBLIC_HOST = os.environ.get("HS_PUBLIC_HOST", "headscale.your-domain.example.com")
# TLS cert probe target. The Caddy edge publishes :8443 on the HOST loopback
# (127.0.0.1:18443), which a container cannot reach via its own 127.0.0.1 — but
# both containers share metamcp-network, so we dial Caddy by service name and
# present the public SNI so the LE cert validates. (edge profile: when Caddy is
# down the probe degrades to severity 'unknown'.)
HS_TLS_PROBE   = (os.environ.get("HS_TLS_PROBE_HOST", "caddy-headscale"),
                  int(os.environ.get("HS_TLS_PROBE_PORT", "8443")))
# The tailscale client we run diagnostics from (joined to the tailnet).
DIAG_TS_CONTAINER = os.environ.get("DIAG_TS_CONTAINER", "tailscale-pia")

# Long-lived TTL cache (the shared _cache is a fixed 20s; version/GitHub wants 6h).
_ttl_cache: dict[str, dict] = {}


def _ttl_get(key: str):
    e = _ttl_cache.get(key)
    if e and time.time() < e["exp"]:
        return e["v"]
    return None


def _ttl_set(key: str, value, ttl: float):
    _ttl_cache[key] = {"v": value, "exp": time.time() + ttl}


def _ts_to_unix(v) -> "int | None":
    """Normalise a headscale timestamp (protobuf {seconds,nanos} object, ISO
    string, or epoch number) to a unix int. Returns None for unset/zero-time."""
    if v is None or v == "":
        return None
    if isinstance(v, dict):
        try:
            s = int(v.get("seconds"))
        except (TypeError, ValueError):
            return None
        return s if s > 0 else None
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def _ts_to_iso(v) -> str:
    u = _ts_to_unix(v)
    return datetime.fromtimestamp(u, tz=timezone.utc).isoformat() if u is not None else ""


async def _notify_send(title: str, body: str, channel: str = "both") -> bool:
    """Best-effort push to the shared notification bridge. We hold
    OWNER_AUTH_TOKEN, so background tasks self-issue an owner cookie to
    authenticate without an inbound request. Never raises."""
    if not OWNER_AUTH_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as c:
            r = await c.post(f"{NOTIFY_BRIDGE_URL}/api/notify",
                             headers={"Cookie": f"{COOKIE_NAME}={_issue_cookie()}"},
                             json={"title": title, "body": body, "channel": channel})
        return r.status_code < 400
    except Exception as e:
        logger.warning(f"notify_send failed: {e}")
        return False


def _hs_sighup() -> bool:
    """Send SIGHUP to headscale — reloads file-mode ACL policy without a restart."""
    try:
        pr = subprocess.run(["docker", "kill", "--signal=HUP", "headscale"],
                            capture_output=True, text=True, timeout=8)
        return pr.returncode == 0
    except Exception as e:
        logger.warning(f"headscale SIGHUP failed: {e}")
        return False


# ── 🅐 Console: per-node detail ──────────────────────────────────────────────
@app.get("/api/headscale/node/{node_id}")
async def api_hs_node_detail(node_id: str, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    nid = _hs_node_id(node_id)
    if nid is None:
        return JSONResponse({"error": "bad_node_id"}, status_code=400)
    rc, out, err = _docker_exec("headscale", ["headscale", "nodes", "list", "--output", "json"])
    if rc != 0:
        return JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                            status_code=502)
    try:
        nodes = json.loads(out) if out.strip() else []
    except Exception:
        return JSONResponse({"error": "parse"}, status_code=502)
    node = next((n for n in nodes if str(n.get("id")) == nid), None)
    if node is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    ips = node.get("ip_addresses") or []
    pak = node.get("pre_auth_key") or {}
    rc2, out2, _ = _docker_exec("headscale", ["headscale", "nodes", "list-routes",
                                              "-i", nid, "--output", "json"])
    try:
        routes = json.loads(out2) if (rc2 == 0 and out2.strip()) else []
    except Exception:
        routes = []
    detail = {
        "id":             str(node.get("id")),
        "name":           node.get("name"),
        "given_name":     node.get("given_name") or "",
        "user":           (node.get("user") or {}).get("name") if isinstance(node.get("user"), dict) else node.get("user"),
        "ipv4":           next((ip for ip in ips if ":" not in (ip or "")), None),
        "ipv6":           next((ip for ip in ips if ":" in (ip or "")), None),
        "online":         bool(node.get("online")),
        "last_seen":      _ts_to_iso(node.get("last_seen")),
        "created_at":     _ts_to_iso(node.get("created_at")),
        "expiry":         _ts_to_iso(node.get("expiry")),
        "register_method": node.get("register_method"),
        "machine_key":    (node.get("machine_key") or "")[:28],
        "node_key":       (node.get("node_key") or "")[:28],
        "disco_key":      (node.get("disco_key") or "")[:28],
        "forced_tags":    node.get("forced_tags") or [],
        "valid_tags":     node.get("valid_tags") or [],
        "preauth_key_id": pak.get("id"),
        "preauth_expiry": _ts_to_iso(pak.get("expiration")),
        "routes":         routes,
    }
    return JSONResponse(detail)


# ── 🅐 Console: connectivity diagnostics (netcheck / DERP latency) ───────────
def _parse_netcheck(text: str) -> dict:
    out: dict[str, Any] = {"udp": None, "ipv4": "", "ipv6": "", "nearest": "",
                           "mapping_varies": None, "derp": []}
    derp_re = re.compile(r"^\s*-\s*([A-Za-z0-9]+):\s*(?:([0-9.]+)\s*ms)?\s*\(([^)]+)\)")
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("* UDP:"):
            out["udp"] = "true" in s.lower()
        elif s.startswith("* IPv4:"):
            out["ipv4"] = s.split(":", 1)[1].strip()
        elif s.startswith("* IPv6:"):
            out["ipv6"] = s.split(":", 1)[1].strip()
        elif s.startswith("* Nearest DERP:"):
            out["nearest"] = s.split(":", 1)[1].strip()
        elif s.startswith("* MappingVariesByDestIP:"):
            out["mapping_varies"] = "true" in s.lower()
        else:
            m = derp_re.match(ln)
            if m:
                code, lat, name = m.group(1), m.group(2), m.group(3).strip()
                out["derp"].append({
                    "code": code, "name": name,
                    "latency_ms": float(lat) if lat else None,
                    # region 999 is our self-hosted relay ("home" / Sentinel Home DERP)
                    "home": code == "home" or "Sentinel Home" in name,
                })
    return out


@app.get("/api/diag/netcheck")
async def api_diag_netcheck(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    cached = _cache_get("netcheck")
    if cached:
        return JSONResponse(cached)
    rc, out, err = _docker_exec(DIAG_TS_CONTAINER, ["tailscale", "netcheck"], timeout=30)
    if not out.strip():
        return JSONResponse({"error": "netcheck_failed",
                             "detail": (err or "no output")[:300],
                             "source": DIAG_TS_CONTAINER}, status_code=502)
    parsed = _parse_netcheck(out)
    parsed["source"] = DIAG_TS_CONTAINER
    parsed["raw"] = out[-4000:]
    parsed["checked_at"] = datetime.now(timezone.utc).isoformat()
    _cache_set("netcheck", parsed)
    return JSONResponse(parsed)


# ── 🅐 Console: activity timeline (audit events) ─────────────────────────────
@app.get("/api/activity")
async def api_activity(request: Request, limit: int = 100):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 100
    try:
        events = users_db.recent_events(limit=limit)
    except Exception as e:
        return JSONResponse({"error": "db", "detail": str(e)[:200]}, status_code=500)
    return JSONResponse({"events": events, "count": len(events)})


# ── 🅑 Operations: one-click "Add device" QR enrolment ───────────────────────
@app.post("/api/headscale/enroll")
async def api_hs_enroll(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = str(body.get("user", "azfar")).strip() or "azfar"
    if not _HS_USER_RE.fullmatch(user):
        return JSONResponse({"error": "bad_user"}, status_code=400)
    expiration = str(body.get("expiration", "1h")).strip() or "1h"
    if not _HS_EXP_RE.fullmatch(expiration):
        return JSONResponse({"error": "bad_expiration"}, status_code=400)
    raw_tags = body.get("tags") or []
    tags = [str(t).strip() for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else []
    if any(not _HS_TAG_RE.fullmatch(t) for t in tags):
        return JSONResponse({"error": "bad_tags"}, status_code=400)
    uid = _hs_user_id(user)
    if uid is None:
        return JSONResponse({"error": "unknown_user", "detail": user}, status_code=400)
    # Enrolment keys are SHORT-LIVED + single-use by design — never reusable.
    cmd = ["headscale", "preauthkeys", "create", "--user", str(uid),
           "--expiration", expiration, "--output", "json"]
    if bool(body.get("ephemeral")):
        cmd.append("--ephemeral")
    if tags:
        cmd += ["--tags", ",".join(tags)]
    rc, out, err = _docker_exec("headscale", cmd)
    if rc != 0:
        return JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                            status_code=502)
    try:
        parsed = json.loads(out)
        key = parsed.get("key") if isinstance(parsed, dict) else ""
    except Exception:
        key = out.strip().splitlines()[-1] if out.strip() else ""
    if not key:
        return JSONResponse({"error": "no_key", "detail": (out or "")[:300]}, status_code=502)
    _cache.pop("hs", None)
    join_cmd = f"tailscale up --login-server https://{HS_PUBLIC_HOST}:8443 --authkey {key} --reset"
    qr_svg = ""
    try:
        import segno
        qr_svg = segno.make(join_cmd, error="m").svg_inline(scale=4, border=2,
                                                            dark="#e8e8ea", light="#1c1c1e")
    except Exception as e:
        logger.warning(f"qr generation failed: {e}")
    p = _get_payload(request) or {}
    try:
        users_db.log_event("headscale.enroll", user_id=p.get("user_id"), jti=p.get("jti") or None,
                           ip=_client_ip(request),
                           payload={"hs_user": user, "expiration": expiration, "tags": tags,
                                    "ephemeral": bool(body.get("ephemeral"))})
    except Exception:
        pass
    return JSONResponse({"ok": True, "key": key, "user": user, "expiration": expiration,
                         "cmd": join_cmd, "qr_svg": qr_svg})


# ── 🅑 Operations: ACL editor (validate → snapshot → write → SIGHUP) ─────────
@app.get("/api/headscale/acl/raw")
async def api_hs_acl_raw(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    if HS_ACL_PATH.exists():
        try:
            return JSONResponse({"policy": HS_ACL_PATH.read_text(encoding="utf-8"),
                                 "source": "file", "path": "/etc/headscale/acl.json",
                                 "editable": True})
        except Exception as e:
            return JSONResponse({"error": "read_failed", "detail": str(e)[:200]}, status_code=500)
    rc, out, err = _docker_exec("headscale", ["headscale", "policy", "get"])
    if rc != 0:
        return JSONResponse({"error": "headscale_failed", "detail": (err or out or "")[:300]},
                            status_code=502)
    return JSONResponse({"policy": out, "source": "cli", "editable": False})


def _acl_check(content: str) -> "tuple[bool, str]":
    """Validate a policy blob with `headscale policy check` against a temp file
    inside the shared config dir (so the headscale binary can read it)."""
    if not HS_CONFIG_DIR.exists():
        return False, "headscale-config mount missing"
    tmp_name = f".acl_check_{int(time.time() * 1000)}.hujson"
    tmp_path = HS_CONFIG_DIR / tmp_name
    try:
        tmp_path.write_text(content, encoding="utf-8")
        rc, out, err = _docker_exec("headscale", ["headscale", "policy", "check", "-f",
                                                  f"/etc/headscale/{tmp_name}"])
    except Exception as e:
        return False, str(e)[:300]
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    if rc != 0:
        return False, (err or out or "policy check failed")[:500]
    return True, (out or "Policy is valid.").strip()[:300]


@app.post("/api/headscale/acl/check")
async def api_hs_acl_check(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    content = body.get("policy")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({"error": "empty_policy"}, status_code=400)
    ok, detail = _acl_check(content)
    return JSONResponse({"ok": ok, "valid": ok, "detail": detail})


@app.post("/api/headscale/acl")
async def api_hs_acl_set(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    if not HS_CONFIG_DIR.exists():
        return JSONResponse({"error": "config_mount_missing",
                             "detail": "headscale-config not mounted; recreate the container."},
                            status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    content = body.get("policy")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({"error": "empty_policy"}, status_code=400)
    if len(content) > 256 * 1024:
        return JSONResponse({"error": "too_large"}, status_code=400)
    ok, detail = _acl_check(content)
    if not ok:
        return JSONResponse({"error": "policy_invalid", "detail": detail}, status_code=400)
    # Snapshot the current acl.json before overwriting (look-before-overwrite).
    backup_rel = ""
    try:
        if HS_ACL_PATH.exists():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bkp = BACKUP_DIR / f"acl-{stamp}.json"
            bkp.write_text(HS_ACL_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            backup_rel = bkp.name
    except Exception as e:
        logger.warning(f"acl backup failed: {e}")
    try:
        HS_ACL_PATH.write_text(content, encoding="utf-8")
    except Exception as e:
        return JSONResponse({"error": "write_failed", "detail": str(e)[:200]}, status_code=500)
    reloaded = _hs_sighup()
    _cache.pop("hs", None)
    p = _get_payload(request) or {}
    try:
        users_db.log_event("headscale.acl.set", user_id=p.get("user_id"), jti=p.get("jti") or None,
                           ip=_client_ip(request), payload={"bytes": len(content), "backup": backup_rel})
    except Exception:
        pass
    return JSONResponse({"ok": True, "backup": backup_rel, "reloaded": reloaded, "detail": detail})


# ── 🅑 Operations: Wake-on-LAN ───────────────────────────────────────────────
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def _load_wol_devices() -> list:
    try:
        if WOL_DEVICES_PATH.exists():
            data = json.loads(WOL_DEVICES_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"wol devices read: {e}")
    return []


def _save_wol_devices(devs: list) -> None:
    WOL_DEVICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    WOL_DEVICES_PATH.write_text(json.dumps(devs, indent=2), encoding="utf-8")


def _send_magic_packet(mac: str) -> int:
    """Broadcast a WoL magic packet. Best-effort: a bridge-networked container
    may not reach the physical LAN broadcast, so we fire at the global and the
    LAN-directed broadcast on the two conventional WoL ports."""
    import socket
    clean = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(clean) != 12:
        return 0
    payload = b"\xff" * 6 + bytes.fromhex(clean) * 16
    sent = 0
    targets = [("255.255.255.255", 9), ("255.255.255.255", 7), ("192.168.50.255", 9)]
    for bcast, port in targets:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(payload, (bcast, port))
            s.close()
            sent += 1
        except Exception:
            pass
    return sent


@app.get("/api/wol/devices")
async def api_wol_devices_list(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    return JSONResponse({"devices": _load_wol_devices()})


@app.post("/api/wol/devices")
async def api_wol_devices_edit(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = str(body.get("action", "add"))
    devs = _load_wol_devices()
    if action == "add":
        name = str(body.get("name", "")).strip()[:48]
        mac = str(body.get("mac", "")).strip()
        if not name or not _MAC_RE.fullmatch(mac):
            return JSONResponse({"error": "bad_input"}, status_code=400)
        mac = mac.upper().replace("-", ":")
        ip = str(body.get("ip", "")).strip()[:64]
        devs = [d for d in devs if d.get("mac") != mac]
        devs.append({"name": name, "mac": mac, "ip": ip})
    elif action == "remove":
        mac = str(body.get("mac", "")).strip().upper().replace("-", ":")
        devs = [d for d in devs if d.get("mac") != mac]
    else:
        return JSONResponse({"error": "bad_action"}, status_code=400)
    _save_wol_devices(devs)
    return JSONResponse({"ok": True, "devices": devs})


@app.post("/api/wol")
async def api_wol_wake(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    mac = str(body.get("mac", "")).strip()
    if not _MAC_RE.fullmatch(mac):
        return JSONResponse({"error": "bad_mac"}, status_code=400)
    sent = _send_magic_packet(mac)
    p = _get_payload(request) or {}
    try:
        users_db.log_event("wol.wake", user_id=p.get("user_id"), jti=p.get("jti") or None,
                           ip=_client_ip(request), payload={"mac": mac.upper(), "packets": sent})
    except Exception:
        pass
    return JSONResponse({"ok": sent > 0, "packets_sent": sent,
                         "note": "Magic packet sent to LAN broadcast. If the device does not "
                                 "wake, the container could not reach the physical LAN broadcast — "
                                 "wake from a host-LAN tool instead."})


# ── 🅒 Reliability: version / update monitor ─────────────────────────────────
def _ver_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


async def _github_latest_release(repo: str) -> dict:
    cached = _ttl_get(f"gh:{repo}")
    if cached is not None:
        return cached
    result: dict = {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"https://api.github.com/repos/{repo}/releases/latest",
                            headers={"accept": "application/vnd.github+json"})
        if r.status_code == 200:
            j = r.json()
            result = {"tag": j.get("tag_name"), "name": j.get("name"),
                      "url": j.get("html_url"), "published_at": j.get("published_at")}
    except Exception as e:
        logger.warning(f"github latest {repo}: {e}")
    if result:
        _ttl_set(f"gh:{repo}", result, 6 * 3600)
    return result


@app.get("/api/monitor/version")
async def api_monitor_version(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    rc, out, _ = _docker_exec("headscale", ["headscale", "version"])
    current = (out or "").strip().splitlines()[0].strip() if out.strip() else ""
    latest = await _github_latest_release("juanfont/headscale")
    latest_tag = (latest.get("tag") or "").lstrip("v")
    update = bool(current and latest_tag and _ver_tuple(latest_tag) > _ver_tuple(current))
    return JSONResponse({
        "component": "headscale", "current": current, "latest": latest_tag,
        "latest_url": latest.get("url"), "published_at": latest.get("published_at"),
        "update_available": update, "checked_at": datetime.now(timezone.utc).isoformat(),
    })


# ── 🅒 Reliability: expiry + TLS cert monitor ────────────────────────────────
def _tls_cert_expiry(host: str, addr: tuple) -> dict:
    import socket
    import ssl
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection(addr, timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
        not_after = cert.get("notAfter")
        exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (exp - datetime.now(timezone.utc)).total_seconds() / 86400.0
        issuer = dict(x[0] for x in cert.get("issuer", []) if x).get("organizationName", "")
        return {"ok": True, "not_after": exp.isoformat(), "days_left": round(days, 1), "issuer": issuer}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


def _severity(days) -> str:
    if days is None:
        return "unknown"
    if days < 7:
        return "critical"
    if days < 30:
        return "warn"
    return "ok"


def _gather_expiry() -> dict:
    items = []
    tls = _tls_cert_expiry(HS_PUBLIC_HOST, HS_TLS_PROBE)
    items.append({
        "kind": "tls_cert", "name": f"{HS_PUBLIC_HOST} TLS cert",
        "days_left": tls.get("days_left") if tls.get("ok") else None,
        "detail": tls.get("issuer", "") if tls.get("ok") else f"unreachable: {tls.get('error', '')}",
        "severity": _severity(tls.get("days_left")) if tls.get("ok") else "unknown",
    })
    rc, out, _ = _docker_exec("headscale", ["headscale", "nodes", "list", "--output", "json"])
    if rc == 0 and out.strip():
        try:
            nodes = json.loads(out)
        except Exception:
            nodes = []
        now = time.time()
        for n in nodes:
            ev = _ts_to_unix(n.get("expiry"))
            if ev:
                d = round((ev - now) / 86400.0, 1)
                items.append({"kind": "node_key",
                              "name": n.get("name") or n.get("given_name") or str(n.get("id")),
                              "days_left": d, "detail": "node key expiry", "severity": _severity(d)})
    order = {"critical": 0, "warn": 1, "unknown": 2, "ok": 3}
    worst = min((i["severity"] for i in items), key=lambda s: order.get(s, 3), default="ok")
    return {"items": items, "worst": worst, "checked_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/monitor/expiry")
async def api_monitor_expiry(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    data = await asyncio.to_thread(_gather_expiry)
    return JSONResponse(data)


# ── 🅒 Reliability: control-plane backup / restore ───────────────────────────
_BKP_NAME_RE = re.compile(r"^hs-[0-9A-Za-z._-]{1,40}$")


def _do_backup(prefix: str = "hs") -> dict:
    """Snapshot the headscale control-plane SoT: sqlite DB (+ wal/shm) via
    `docker cp`, plus config.yaml + acl.json from the shared mount."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"{prefix}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    copied, errors = [], []
    for fn in ("db.sqlite", "db.sqlite-wal", "db.sqlite-shm"):
        try:
            pr = subprocess.run(["docker", "cp", f"headscale:/var/lib/headscale/{fn}",
                                 str(dest / fn)], capture_output=True, text=True, timeout=40)
            if pr.returncode == 0:
                copied.append(fn)
            elif fn == "db.sqlite":
                errors.append(f"{fn}: {(pr.stderr or '').strip()[:120]}")
        except Exception as e:
            if fn == "db.sqlite":
                errors.append(f"{fn}: {str(e)[:120]}")
    for src in (HS_CONFIG_YAML, HS_ACL_PATH):
        try:
            if src.exists():
                (dest / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                copied.append(src.name)
        except Exception as e:
            errors.append(f"{src.name}: {str(e)[:120]}")
    ok = "db.sqlite" in copied
    return {"ok": ok, "name": dest.name, "files": copied, "errors": errors}


@app.get("/api/backup/list")
async def api_backup_list(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    out = []
    if BACKUP_DIR.exists():
        for p in sorted(BACKUP_DIR.iterdir(), reverse=True):
            try:
                if p.is_dir() and (p.name.startswith("hs-") or p.name.startswith("pre-restore-")):
                    files = [f.name for f in p.iterdir() if f.is_file()]
                    size = sum(f.stat().st_size for f in p.iterdir() if f.is_file())
                    out.append({"name": p.name, "files": files, "size": size,
                                "created": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()})
            except Exception:
                pass
    return JSONResponse({"backups": out, "dir": str(BACKUP_DIR)})


@app.post("/api/backup/create")
async def api_backup_create(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    res = await asyncio.to_thread(_do_backup, "hs")
    if not res["ok"]:
        return JSONResponse({"error": "backup_failed", "detail": "; ".join(res["errors"])[:300]},
                            status_code=502)
    p = _get_payload(request) or {}
    try:
        users_db.log_event("backup.create", user_id=p.get("user_id"), jti=p.get("jti") or None,
                           ip=_client_ip(request), payload={"name": res["name"], "files": res["files"]})
    except Exception:
        pass
    return JSONResponse(res)


@app.get("/api/backup/{name}/download")
async def api_backup_download(name: str, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    if not _BKP_NAME_RE.fullmatch(name) and not re.fullmatch(r"pre-restore-[0-9A-Za-z._-]{1,40}", name):
        return JSONResponse({"error": "bad_name"}, status_code=400)
    src = BACKUP_DIR / name
    if not src.is_dir():
        return JSONResponse({"error": "not_found"}, status_code=404)
    import zipfile
    zpath = BACKUP_DIR / f"{name}.zip"
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in src.iterdir():
                if f.is_file():
                    zf.write(f, arcname=f.name)
    except Exception as e:
        return JSONResponse({"error": "zip_failed", "detail": str(e)[:200]}, status_code=500)
    return FileResponse(str(zpath), filename=f"{name}.zip", media_type="application/zip")


@app.post("/api/backup/restore")
async def api_backup_restore(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()
    if not (_BKP_NAME_RE.fullmatch(name) or re.fullmatch(r"pre-restore-[0-9A-Za-z._-]{1,40}", name)):
        return JSONResponse({"error": "bad_name"}, status_code=400)
    src = BACKUP_DIR / name
    db = src / "db.sqlite"
    if not db.exists():
        return JSONResponse({"error": "db_not_in_backup"}, status_code=404)

    def _restore() -> dict:
        # 1) Mandatory snapshot of the CURRENT state before we overwrite it.
        pre = _do_backup("pre-restore")
        # 2) Stop headscale so the SQLite file is not open during the swap.
        subprocess.run(["docker", "stop", "headscale"], capture_output=True, text=True, timeout=45)
        # 3) Copy the DB (+ wal/shm) back. To avoid a stale WAL replaying over a
        #    restored DB, neutralise wal/shm with the backup's copies, or empty
        #    files when the snapshot was checkpointed (no wal present).
        import tempfile
        cp_errors = []
        for fn in ("db.sqlite", "db.sqlite-wal", "db.sqlite-shm"):
            f = src / fn
            try:
                if f.exists():
                    pr = subprocess.run(["docker", "cp", str(f),
                                         f"headscale:/var/lib/headscale/{fn}"],
                                        capture_output=True, text=True, timeout=40)
                    if pr.returncode != 0:
                        cp_errors.append(f"{fn}: {(pr.stderr or '').strip()[:120]}")
                elif fn in ("db.sqlite-wal", "db.sqlite-shm"):
                    # Overwrite any leftover wal/shm with an empty file.
                    tf = Path(tempfile.gettempdir()) / fn
                    tf.write_bytes(b"")
                    subprocess.run(["docker", "cp", str(tf),
                                    f"headscale:/var/lib/headscale/{fn}"],
                                   capture_output=True, text=True, timeout=40)
            except Exception as e:
                cp_errors.append(f"{fn}: {str(e)[:120]}")
        # 4) Start headscale + verify it answers.
        subprocess.run(["docker", "start", "headscale"], capture_output=True, text=True, timeout=45)
        verify_rc = -1
        for _ in range(6):
            time.sleep(2)
            verify_rc, _o, _e = _docker_exec("headscale", ["headscale", "users", "list"])
            if verify_rc == 0:
                break
        return {"ok": verify_rc == 0, "pre_restore_backup": pre.get("name"),
                "cp_errors": cp_errors, "verified": verify_rc == 0}

    res = await asyncio.to_thread(_restore)
    _cache.pop("hs", None)
    p = _get_payload(request) or {}
    try:
        users_db.log_event("backup.restore", user_id=p.get("user_id"), jti=p.get("jti") or None,
                           ip=_client_ip(request), payload={"name": name, "ok": res["ok"],
                                                            "pre": res.get("pre_restore_backup")})
    except Exception:
        pass
    status = 200 if res["ok"] else 502
    return JSONResponse(res, status_code=status)


# ── 🅒 Reliability: DNS / MagicDNS management ─────────────────────────────────
def _replace_top_level_block(text: str, key: str, new_block: str) -> str:
    """Replace a top-level YAML `key:` block (until the next column-0 line or a
    blank line) with new_block, preserving everything else — including the
    comments around other sections."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", ln):
            start = i
            break
    if start is None:
        return text.rstrip("\n") + "\n\n" + new_block.rstrip("\n") + "\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() == "" or not ln[:1].isspace():
            end = j
            break
    block = new_block if new_block.endswith("\n") else new_block + "\n"
    return "".join(lines[:start]) + block + "".join(lines[end:])


@app.get("/api/dns")
async def api_dns_get(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    if not HS_CONFIG_YAML.exists():
        return JSONResponse({"error": "config_mount_missing"}, status_code=503)
    try:
        cfg = yaml.safe_load(HS_CONFIG_YAML.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return JSONResponse({"error": "parse", "detail": str(e)[:200]}, status_code=500)
    dns = cfg.get("dns") or {}
    ns = dns.get("nameservers") or {}
    return JSONResponse({
        "magic_dns": bool(dns.get("magic_dns", False)),
        "override_local_dns": bool(dns.get("override_local_dns", False)),
        "base_domain": dns.get("base_domain") or cfg.get("base_domain") or "",
        "global_nameservers": ns.get("global") or [],
        "split_nameservers": ns.get("split") or {},
        "search_domains": dns.get("search_domains") or [],
        "extra_records": dns.get("extra_records") or [],
    })


@app.post("/api/dns")
async def api_dns_set(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    if not HS_CONFIG_YAML.exists():
        return JSONResponse({"error": "config_mount_missing"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    magic = bool(body.get("magic_dns", True))
    override = bool(body.get("override_local_dns", False))
    base = str(body.get("base_domain", "tail.your-domain.example.com")).strip()
    dom_re = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
    if not dom_re.fullmatch(base):
        return JSONResponse({"error": "bad_base_domain"}, status_code=400)
    ip_re = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")
    g_ns = body.get("global_nameservers") or []
    g_ns = [str(x).strip() for x in g_ns if str(x).strip()] if isinstance(g_ns, list) else []
    if any(not ip_re.fullmatch(x) for x in g_ns):
        return JSONResponse({"error": "bad_nameservers"}, status_code=400)
    sd = body.get("search_domains") or []
    sd = [str(x).strip() for x in sd if str(x).strip()] if isinstance(sd, list) else []
    if any(not dom_re.fullmatch(x) for x in sd):
        return JSONResponse({"error": "bad_search_domains"}, status_code=400)
    original = HS_CONFIG_YAML.read_text(encoding="utf-8")
    nl = ["dns:",
          f"  override_local_dns: {'true' if override else 'false'}",
          f"  magic_dns: {'true' if magic else 'false'}",
          f"  base_domain: {base}",
          "  nameservers:"]
    if g_ns:
        nl.append("    global:")
        nl += [f"      - {x}" for x in g_ns]
    else:
        nl.append("    global: []")
    if sd:
        nl.append("  search_domains:")
        nl += [f"    - {x}" for x in sd]
    else:
        nl.append("  search_domains: []")
    new_block = "\n".join(nl) + "\n"
    updated = _replace_top_level_block(original, "dns", new_block)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"config-{stamp}.yaml"
    (BACKUP_DIR / backup_name).write_text(original, encoding="utf-8")
    HS_CONFIG_YAML.write_text(updated, encoding="utf-8")
    rc, out, err = _docker_exec("headscale", ["headscale", "configtest"])
    if rc != 0:
        HS_CONFIG_YAML.write_text(original, encoding="utf-8")  # rollback
        return JSONResponse({"error": "configtest_failed", "rolled_back": True,
                             "detail": (err or out or "")[:500]}, status_code=400)
    # DNS/derp config (unlike ACL policy) only applies on a full restart.
    restarted = False
    try:
        pr = subprocess.run(["docker", "restart", "headscale"], capture_output=True, text=True, timeout=60)
        restarted = pr.returncode == 0
    except Exception as e:
        logger.warning(f"headscale restart failed: {e}")
    _cache.pop("hs", None)
    p = _get_payload(request) or {}
    try:
        users_db.log_event("headscale.dns.set", user_id=p.get("user_id"), jti=p.get("jti") or None,
                           ip=_client_ip(request),
                           payload={"magic_dns": magic, "global_ns": g_ns, "search_domains": sd,
                                    "backup": backup_name})
    except Exception:
        pass
    return JSONResponse({"ok": True, "restarted": restarted, "backup": backup_name})


# ── Background: daily expiry/cert alert (SURFACE=network only, dedup per day) ─
async def _expiry_alert_loop():
    alerted: dict[str, str] = {}
    await asyncio.sleep(120)  # let the stack settle after boot
    while True:
        try:
            data = await asyncio.to_thread(_gather_expiry)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            for it in data.get("items", []):
                d = it.get("days_left")
                if d is not None and d < 7:
                    key = f"{it['kind']}:{it['name']}"
                    if alerted.get(key) != today:
                        await _notify_send("⚠ Sentinel Network — expiry",
                                           f"{it['name']} expires in {d:.0f} day(s).")
                        alerted[key] = today
        except Exception as e:
            logger.warning(f"expiry alert loop: {e}")
        await asyncio.sleep(6 * 3600)


HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Sentinel Network</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#1c1c1e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>
:root { /* SMDL "Chrome · Cyan" palette — matched 1:1 to the Sentinel Network
           desktop app (sentinel-network/src/app.css). Seed:
           sentinel-smdl/app/theme_tokens.json. */
        --bg:#07080a; --fg:#e6edf3; --muted:#8b97a6; --section:#14171c;
        --sep:#2a2f37; --pos:#34c759; --warn:#ff9f0a; --neg:#ff453a;
        --accent:#2af6ff; --accent-2:#15c2d4; --accent-rgb:42,246,255;
        --button-text:#04141a; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { margin:0; background:var(--bg); color:var(--fg);
       padding: calc(14px + env(safe-area-inset-top)) calc(12px + env(safe-area-inset-right))
                calc(40px + env(safe-area-inset-bottom)) calc(12px + env(safe-area-inset-left));
       font:14px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       max-width:760px; margin-left:auto; margin-right:auto; }
header { display:flex; justify-content:space-between; align-items:center;
         padding:4px 2px 16px; }
h1 { margin:0; font-size:18px; font-weight:700; }
.refresh { font-size:11px; color:var(--muted); }
.route { background:var(--section); border:1px solid var(--sep); border-radius:12px;
         padding:14px; margin:10px 0; }
.route h2 { margin:0 0 6px; font-size:14px; font-weight:600; display:flex;
            align-items:center; gap:8px; }
.dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
.dot.up   { background:var(--pos); box-shadow:0 0 6px rgba(52,199,89,0.4); }
.dot.warn { background:var(--warn); }
.dot.down { background:var(--neg); }
.route .sub { color:var(--muted); font-size:11px; margin-bottom:8px; }
.route table { width:100%; border-collapse:collapse; font-size:12px; margin-top:4px; }
.route th, .route td { padding:5px 6px; text-align:left; border-bottom:1px solid var(--sep);
                       vertical-align:top; }
.route th { color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase;
            letter-spacing:0.4px; }
.route tr:last-child td { border-bottom:none; }
.route .url { font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:11px; }
.route .age { color:var(--muted); font-size:11px; }
.empty { color:var(--muted); padding:10px 0; font-size:12px; }
.tag { display:inline-block; padding:1px 6px; border-radius:8px; font-size:10px;
       font-weight:600; }
.tag.up   { background:rgba(52,199,89,0.18);  color:var(--pos); }
.tag.warn { background:rgba(255,204,0,0.18);  color:var(--warn); }
.tag.down { background:rgba(255,69,58,0.18);  color:var(--neg); }
footer { color:var(--muted); font-size:10px; text-align:center; margin-top:24px; }
.spin { display:inline-block; width:10px; height:10px; border:2px solid var(--muted);
        border-top-color:var(--accent); border-radius:50%; animation:sp 1s linear infinite; }
@keyframes sp { to { transform:rotate(360deg); } }
.ctl { margin-top:10px; border-top:1px dashed var(--sep); padding-top:10px; }
.ctl summary { cursor:pointer; color:var(--accent); font-size:12px; font-weight:600;
               list-style:none; }
.ctl summary::-webkit-details-marker { display:none; }
.ctl .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:10px; }
.ctl select, .ctl input[type=text] { background:#1c1c1e; color:var(--fg);
        border:1px solid var(--sep); border-radius:8px; padding:7px 9px; font-size:12px; }
.ctl input[type=text] { width:64px; }
.ctl label.cb { font-size:12px; color:var(--muted); display:flex; align-items:center; gap:4px; }
.btn { background:var(--accent); color:var(--button-text); border:none; border-radius:8px;
       padding:7px 14px; font-size:12px; font-weight:600; cursor:pointer; }
.btn:disabled { opacity:0.5; cursor:default; }
#keyout .keybox { background:var(--section); border:1px solid var(--pos); border-radius:12px;
       padding:12px 14px; margin:10px 0; }
#keyout .keybox.err { border-color:var(--neg); }
#keyout .keyval { font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:12px;
       word-break:break-all; background:#1c1c1e; padding:8px 10px; border-radius:8px;
       margin:8px 0; user-select:all; }
.keyval { font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:12px;
       word-break:break-all; background:#1c1c1e; padding:8px 10px; border-radius:8px; }
.nodeact { white-space:nowrap; }
.mini { background:#3a3a3c; color:var(--fg); border:none; border-radius:7px; padding:7px 10px;
        font-size:14px; line-height:1; cursor:pointer; margin:2px; min-width:36px; }
.mini.danger { background:rgba(255,69,58,0.25); }
.mini:hover { background:#4a4a4c; }
.mini:active { transform:scale(0.94); }
/* Phones: let wide tables scroll horizontally instead of overflowing the page,
   and give controls room to wrap. */
@media (max-width:600px) {
  .route table { display:block; width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .route thead, .route tbody { display:table; width:100%; min-width:max-content; }
  .route th, .route td { white-space:nowrap; }
  .ctl input[type=text] { flex:1 1 auto; min-width:0; }
  .btn { padding:9px 14px; }
}
/* ── top-nav tabbed shell + right action rail (IA refactor 2026-06-04) ── */
.topnav { display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:40;
          background:var(--bg); border-bottom:1px solid var(--sep);
          padding:8px 2px; margin:-14px -2px 12px; flex-wrap:wrap; }
.topnav .brand { font-size:16px; font-weight:700; white-space:nowrap; }
.topnav .tabs { display:flex; gap:4px; flex-wrap:wrap; }
.tab { background:none; border:none; color:var(--muted); font:600 13px/1 inherit;
       padding:7px 11px; border-radius:8px; cursor:pointer; }
.tab.active { background:var(--section); color:var(--fg); }
.tab:hover { color:var(--fg); }
.topnav .refresh { margin-left:auto; }
.layout { display:flex; gap:10px; align-items:flex-start; }
.svcnav { display:flex; flex-direction:column; gap:2px; width:152px; flex:none;
          position:sticky; top:58px; }
.svcbtn { display:flex; flex-direction:column; align-items:flex-start; gap:1px; background:none;
          border:1px solid transparent; border-radius:8px; padding:7px 10px; cursor:pointer; text-align:left; }
.svcbtn .l { color:var(--fg); font:600 13px/1.2 inherit; }
.svcbtn .s { color:var(--muted); font-size:10px; }
.svcbtn:hover { background:var(--section); }
.svcbtn.active { background:var(--section); border-color:var(--accent); }
.content { flex:1; min-width:0; }
.subnav { display:flex; gap:2px; border-bottom:1px solid var(--sep); margin:0 0 12px; flex-wrap:wrap; }
.subbtn { background:none; border:none; border-bottom:2px solid transparent; border-radius:0;
          color:var(--muted); font:600 13px/1 inherit; padding:8px 12px; cursor:pointer; }
.subbtn:hover { color:var(--fg); }
.subbtn.active { color:var(--accent); border-bottom-color:var(--accent); }
.rightrail { display:flex; flex-direction:column; gap:8px; position:sticky; top:58px; }
.rail { width:42px; height:42px; border-radius:11px; background:var(--section);
        border:1px solid var(--sep); color:var(--fg); font-size:18px; cursor:pointer; }
.rail:hover { background:#3a3a3c; }
.rail.active { border-color:var(--accent); color:var(--accent); }
.panel { display:contents; }
.hide { display:none !important; }
@keyframes fade { from { opacity:0; transform:translateY(4px); } }
@media (max-width:600px) {
  .layout { flex-direction:column; }
  .svcnav { flex-direction:row; width:100%; overflow-x:auto; position:static; padding-bottom:4px; }
  .svcbtn { flex:none; }
  .svcbtn .s { display:none; }
  .rightrail { position:fixed; right:8px; flex-direction:row;
               bottom:calc(10px + env(safe-area-inset-bottom)); top:auto; z-index:45;
               background:rgba(28,28,30,.85); backdrop-filter:blur(8px);
               padding:6px; border:1px solid var(--sep); border-radius:14px; }
}
/* ── feature widgets: cards, kv grid, severity, drawer, modal, editor ── */
.card { background:var(--section); border:1px solid var(--sep); border-radius:12px;
        padding:14px; margin:10px 0; }
.card h2 { margin:0 0 8px; font-size:14px; font-weight:600; display:flex;
           align-items:center; gap:8px; }
.card h2 .act { margin-left:auto; display:flex; gap:6px; }
.kv { display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; font-size:12px; }
.kv .k { color:var(--muted); }
.kv .v { word-break:break-word; }
.kv .v.mono { font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:11px; }
.sev { display:inline-block; padding:1px 7px; border-radius:8px; font-size:10px; font-weight:700; }
.sev.ok { background:rgba(52,199,89,0.18); color:var(--pos); }
.sev.warn { background:rgba(255,204,0,0.18); color:var(--warn); }
.sev.critical { background:rgba(255,69,58,0.18); color:var(--neg); }
.sev.unknown { background:rgba(142,142,147,0.18); color:var(--muted); }
.barwrap { height:6px; background:#1c1c1e; border-radius:4px; overflow:hidden; margin-top:3px; }
.bar { height:100%; border-radius:4px; }
.timeline { font-size:12px; }
.tl-item { display:flex; gap:10px; padding:8px 2px; border-bottom:1px solid var(--sep); }
.tl-item:last-child { border-bottom:none; }
.tl-ev { font-weight:600; }
.tl-meta { color:var(--muted); font-size:11px; }
.tl-dot { width:8px; height:8px; border-radius:50%; background:var(--accent); margin-top:5px; flex:none; }
textarea.code { width:100%; min-height:280px; background:#161618; color:var(--fg);
        border:1px solid var(--sep); border-radius:10px; padding:10px;
        font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:12px; line-height:1.45;
        resize:vertical; white-space:pre; overflow-wrap:normal; }
.note { font-size:11px; color:var(--muted); margin-top:6px; }
.note.ok { color:var(--pos); } .note.err { color:var(--neg); }
input.txt, select.txt { background:#1c1c1e; color:var(--fg); border:1px solid var(--sep);
        border-radius:8px; padding:8px 10px; font-size:13px; }
input.txt { width:100%; }
/* slide-in drawer (node detail) */
.drawer-ov { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:60; display:none; }
.drawer-ov.show { display:block; }
.drawer { position:fixed; top:0; right:0; bottom:0; width:min(440px,94vw); z-index:61;
        background:var(--bg); border-left:1px solid var(--sep); transform:translateX(100%);
        transition:transform .2s ease; overflow-y:auto;
        padding: calc(16px + env(safe-area-inset-top)) 16px calc(20px + env(safe-area-inset-bottom)); }
.drawer.show { transform:translateX(0); }
.drawer .x { position:absolute; top:12px; right:14px; background:none; border:none;
        color:var(--muted); font-size:24px; cursor:pointer; }
.drawer h2 { margin:0 4px 12px 0; font-size:16px; }
/* centered modal (QR enrol) */
.modal-ov { position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:60;
        display:none; align-items:center; justify-content:center; padding:16px; }
.modal-ov.show { display:flex; }
.modal { background:var(--section); border:1px solid var(--sep); border-radius:16px;
        width:min(420px,96vw); max-height:92vh; overflow-y:auto; padding:18px; position:relative; }
.modal .x { position:absolute; top:10px; right:12px; background:none; border:none;
        color:var(--muted); font-size:22px; cursor:pointer; }
.modal h2 { margin:0 0 12px; font-size:16px; }
.qrbox { background:#1c1c1e; border-radius:12px; padding:14px; text-align:center; margin:10px 0; }
.qrbox svg { width:200px; height:200px; }
.fieldrow { display:flex; flex-direction:column; gap:4px; margin:8px 0; }
.fieldrow label { font-size:11px; color:var(--muted); }
.card table { width:100%; border-collapse:collapse; font-size:12px; margin-top:6px; }
.card th, .card td { padding:5px 6px; text-align:left; border-bottom:1px solid var(--sep);
        vertical-align:middle; }
.card th { color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase;
        letter-spacing:0.4px; }
.card tr:last-child td { border-bottom:none; }
.card .sub { color:var(--muted); font-size:11px; margin-bottom:8px; }
.copychip { display:inline-block; font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:12px;
        background:#1c1c1e; border:1px solid var(--sep); border-radius:7px; padding:3px 8px;
        cursor:pointer; white-space:nowrap; }
.copychip:hover { border-color:var(--accent); }
.copychip:active { transform:scale(0.95); }
.copychip.copied { border-color:var(--pos); color:var(--pos); }
</style>
</head><body>

<nav class=topnav>
  <span class=brand>🛡 Sentinel Network</span>
  <span class=refresh id=last-refresh>—</span>
</nav>

<div id=keyout></div>

<div class=layout>
  <aside class=svcnav id=svcnav></aside>
  <main class=content>
    <nav class=subnav id=subnav></nav>
    <section class="panel active" id=panel-overview data-svc=overview>
      <div id=routes><div class=route><div class=empty><span class=spin></span> Loading…</div></div></div>
    </section>
    <section class="panel" id=panel-amneziawg data-svc=amneziawg data-sub=peers>
      <div class=card><h2>🔒 AmneziaWG</h2>
        <div id=awgout><div class=empty><span class=spin></span> Loading…</div></div></div>
    </section>
    <section class="panel" id=panel-router data-svc=router data-sub=forwards>
      <div class=card><h2>📡 Router · port forwards</h2>
        <div class=empty>Managed in the Sentinel Network <b>desktop app → Router</b> (AsusWRT Virtual Server). Read-only router status on this surface is planned.</div></div>
    </section>
    <section class="panel" id=panel-router-upnp data-svc=router data-sub=upnp>
      <div class=card><h2>📡 Router · UPnP</h2>
        <div class=empty>UPnP dynamic mappings — planned (backlog NET-ROUTER-3C).</div></div>
    </section>
    <section class="panel" id=panel-local-ports data-svc=local data-sub=ports>
      <div class=card><h2>🧩 Host ports<span class=act><button class=btn onclick="loadPorts(this)">Refresh</button></span></h2>
        <div class=sub>Listening TCP/UDP ports on the host with the owning process (read-only, via the host watchdog API). Full inventory + docker/native split in the desktop app → Local → Ports.</div>
        <div id=portsout><div class=empty><span class=spin></span> Loading…</div></div></div>
    </section>
    <section class="panel" id=panel-local-fwd data-svc=local data-sub=forwarder>
      <div class=card><h2>↔ Forwarder</h2>
        <div class=empty>The LAN forwarder is a desktop-native feature (binds local ports) — not available on this surface.</div></div>
    </section>
    <section class=panel id=panel-tailnet data-svc=tailnet data-sub="nodes routes">
      <div id=hsnodes><div class=route><div class=empty><span class=spin></span> Loading…</div></div></div>
    </section>
    <section class=panel id=panel-discovery>
      <div class=card id=card-lan data-svc=devices data-sub=discovery>
        <h2>🔎 LAN devices<span class=act><button class=btn onclick="scanLan(this)">Scan LAN</button></span></h2>
        <div class=sub>Ping-sweeps <code>192.168.50.0/24</code> from the host (the container has no LAN/MAC visibility) and reads its ARP table. Tap any IP or MAC to copy. Devices that are off or block ping won’t appear.</div>
        <div id=lanout><div class=empty>Press “Scan LAN”. First scan takes ~3–8s.</div></div>
      </div>
      <div class=card id=card-tn data-svc=devices data-sub=discovery>
        <h2>🕸 Tailnet devices<span class=act><button class=btn onclick="loadDiscoveryTailnet()">Refresh</button></span></h2>
        <div class=sub>Tap a tailnet IP to copy.</div>
        <div id=tnout><div class=empty><span class=spin></span> Loading…</div></div>
      </div>
    </section>
    <section class=panel id=panel-diag>
      <div class=card data-svc=diagnostics data-sub=netcheck>
        <h2>🔍 Connectivity diagnostics<span class=act><button class=btn onclick="runNetcheck(this)">Run netcheck</button></span></h2>
        <div class=sub>Runs <code>tailscale netcheck</code> from the tailnet client and shows DERP relay latency. Region&nbsp;999 (Sentinel Home DERP) is highlighted.</div>
        <div id=diagout><div class=empty>Press “Run netcheck”. First run can take ~5–15s.</div></div>
      </div>
      <div class=card data-svc=diagnostics data-sub=cf><h2>☁ Cloudflare Tunnels</h2>
        <div class=empty>CF tunnel route health is summarised on the <b>Overview</b>. A dedicated view is planned.</div></div>
      <div class=card data-svc=diagnostics data-sub=edge><h2>🌐 Edge check</h2>
        <div class=empty>End-to-end public-URL probe — available in the <b>desktop app → Diagnostics → Edge</b>. A web version is planned.</div></div>
    </section>
    <section class=panel id=panel-activity>
      <div class=card data-svc=maintenance data-sub=activity>
        <h2>📜 Activity timeline<span class=act><button class=btn onclick="loadActivity()">Refresh</button></span></h2>
        <div class=sub>Owner actions on the control plane (preauth keys, node lifecycle, ACL/DNS edits, backups, WoL).</div>
        <div id=activityout><div class=empty><span class=spin></span> Loading…</div></div>
      </div>
    </section>
    <section class=panel id=panel-access>
      <div id=hsctl data-svc=tailnet data-sub=keys></div>
      <div class=card data-svc=tailnet data-sub=acl>
        <h2>📝 ACL policy editor<span class=act>
          <button class=btn onclick="loadACLEditor()">Reload</button>
          <button class=btn onclick="checkACL()">Check</button>
          <button class=btn onclick="saveACL()">Save &amp; reload</button>
        </span></h2>
        <div class=sub>File-mode policy (<code>/etc/headscale/acl.json</code>). “Check” validates with <code>headscale policy check</code>; “Save” snapshots the old file then applies via SIGHUP. Edits here are owner-only and audited.</div>
        <textarea id=acl-editor class=code spellcheck=false placeholder="Loading policy…"></textarea>
        <div id=acl-note class=note></div>
      </div>
    </section>
    <section class=panel id=panel-maint>
      <div class=card id=card-version data-svc=maintenance data-sub=version><h2>⬆ Version<span class=act><button class=btn onclick="loadVersion()">Check</button></span></h2>
        <div id=verout><div class=empty>Press “Check”.</div></div></div>
      <div class=card id=card-expiry data-svc=maintenance data-sub=version><h2>⏳ Expiry &amp; certs<span class=act><button class=btn onclick="loadExpiry()">Check</button></span></h2>
        <div id=expout><div class=empty>Press “Check”.</div></div></div>
      <div class=card id=card-wol data-svc=devices data-sub=wol><h2>🔌 Wake-on-LAN<span class=act><button class=btn onclick="addWolPrompt()">Add device</button></span></h2>
        <div id=wolout><div class=empty><span class=spin></span> Loading…</div></div></div>
      <div class=card id=card-dns data-svc=tailnet data-sub=dns><h2>🌐 DNS / MagicDNS<span class=act><button class=btn onclick="loadDNS()">Reload</button><button class=btn onclick="saveDNS()">Save</button></span></h2>
        <div id=dnsout><div class=empty><span class=spin></span> Loading…</div></div></div>
      <div class=card id=card-backup data-svc=maintenance data-sub=backup><h2>💾 Control-plane backup<span class=act><button class=btn onclick="createBackup(this)">Snapshot now</button></span></h2>
        <div class=sub>Snapshots the headscale SQLite DB + config.yaml + acl.json. Restore stops headscale, snapshots the current state first, swaps the DB, and restarts.</div>
        <div id=backupout><div class=empty><span class=spin></span> Loading…</div></div></div>
    </section>
  </main>
  <aside class=rightrail>
    <button class=rail title="Refresh now" onclick="refresh()">🔄</button>
    <button class=rail title="Tailnet" onclick="switchTab('tailnet')">🕸</button>
    <button class=rail title="Add device (QR)" onclick="openEnroll()">➕</button>
    <button class=rail title="Diagnostics" onclick="switchTab('diag')">🔍</button>
    <button class=rail title="Maintenance" onclick="switchTab('maint')">🛠</button>
  </aside>
</div>

<!-- node detail drawer -->
<div class=drawer-ov id=drawer-ov onclick="closeDrawer()"></div>
<aside class=drawer id=drawer>
  <button class=x onclick="closeDrawer()">×</button>
  <div id=drawer-body></div>
</aside>

<!-- add-device QR modal -->
<div class=modal-ov id=enroll-ov>
  <div class=modal>
    <button class=x onclick="closeEnroll()">×</button>
    <h2>➕ Add a device</h2>
    <div id=enroll-body></div>
  </div>
</div>

<footer>Polling every 30s · By Azfar · Powered by Claude</footer>

<script>
function ago(unix) {
  if (!unix) return '—';
  const s = Math.floor(Date.now()/1000) - unix;
  if (s < 60)    return s + 's ago';
  if (s < 3600)  return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}
function bytes(n) {
  if (!n) return '0 B';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0; while (n>=1024 && i<u.length-1) { n/=1024; i++; }
  return n.toFixed(n<10 && i>0 ? 1 : 0) + ' ' + u[i];
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function renderCF(d) {
  const cls = d.all_up ? 'up' : 'warn';
  const rows = d.routes.map(r => `
    <tr>
      <td><span class="dot ${r.ok?'up':'down'}"></span></td>
      <td><b>${esc(r.label)}</b><br><span class=url>${esc(r.hostname)}</span></td>
      <td><span class=url>${esc(r.backend)}</span></td>
      <td>${r.ok ? r.code : '—'}${r.note ? ` <span class=age>(${esc(r.note)})</span>` : ''}</td>
      <td class=age>${r.latency_ms||0}ms</td>
    </tr>`).join('');
  return `
    <div class=route>
      <h2><span class="dot ${cls}"></span>☁ ${esc(d.label)}<span class="tag ${cls}" style="margin-left:auto">${esc(d.summary)}</span></h2>
      <div class=sub>HTTPS through Cloudflare → cloudflared agent → localhost</div>
      <table>
        <thead><tr><th></th><th>Hostname</th><th>Backend</th><th>Status</th><th>Latency</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderHS(d) {
  const cls = (d.err || d.node_count === 0) ? 'warn' : 'up';
  const rows = (d.nodes||[]).map(n => `
    <tr>
      <td><span class="dot ${n.online?'up':'down'}"></span></td>
      <td><b><a href="javascript:void 0" onclick="nodeDetail('${esc(n.id)}')" style="color:var(--accent);text-decoration:none">${esc(n.name)}</a></b></td>
      <td class=url>${esc(n.ipv4||'')}</td>
      <td>${esc(n.user||'')}</td>
      <td class=age>${n.online ? 'online' : (n.last_seen ? esc(n.last_seen.slice(0,16).replace('T',' ')) : '—')}</td>
      <td class=nodeact>
        <button class=mini title="Routes / exit node" onclick="nodeRoutes('${esc(n.id)}','${esc(n.name)}')">🧭</button>
        <button class=mini title="Rename" onclick="nodeRename('${esc(n.id)}','${esc(n.name)}')">✎</button>
        <button class=mini title="Expire (force re-login)" onclick="nodeExpire('${esc(n.id)}','${esc(n.name)}')">⎋</button>
        <button class="mini danger" title="Delete" onclick="nodeDelete('${esc(n.id)}','${esc(n.name)}')">🗑</button>
      </td>
    </tr>`).join('');
  const usersLine = (d.users||[]).map(u => esc(u.name||'?')).join(', ') || '<span class=age>(no users)</span>';
  const userOpts = (d.users||[]).map(u => `<option value="${esc(u.name||'')}">${esc(u.name||'?')}</option>`).join('');
  const ctl = `
    <details class=ctl>
      <summary>⚙ Generate preauth key</summary>
      <div class=row>
        <select id=hs-user>${userOpts || '<option value="">(no users)</option>'}</select>
        <input id=hs-exp type=text value="1h" title="expiration e.g. 1h, 24h, 7d">
        <input id=hs-tags type=text style="width:150px" placeholder="tags e.g. tag:owner-device">
        <label class=cb><input id=hs-reuse type=checkbox> reusable</label>
        <label class=cb><input id=hs-eph type=checkbox> ephemeral</label>
        <button class=btn onclick="createPreauthKey(this)">Create</button>
      </div>
    </details>`;
  return `
    <div class=route>
      <h2><span class="dot ${cls}"></span>🕸 ${esc(d.label)}<span class="tag ${cls}" style="margin-left:auto">${esc(d.summary)}</span></h2>
      <div class=sub>Users: ${usersLine}</div>
      ${d.err ? `<div class=empty style="color:var(--neg)">⚠ ${esc(d.err)}</div>` : ''}
      ${rows ? `<table>
        <thead><tr><th></th><th>Node</th><th>Tailnet IP</th><th>User</th><th>Last seen</th><th>Actions</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>` : '<div class=empty>No nodes registered yet. Use “Approve pending node” below, or run <code>tailscale up --login-server=https://headscale.your-domain.example.com:8443</code> on a device.</div>'}
      ${ctl}
    </div>`;
}

async function createPreauthKey(btn) {
  const user = (document.getElementById('hs-user')||{}).value || '';
  const expiration = ((document.getElementById('hs-exp')||{}).value || '1h').trim();
  const tags = (((document.getElementById('hs-tags')||{}).value) || '').split(/[\\s,]+/).filter(Boolean);
  const reusable = !!(document.getElementById('hs-reuse')||{}).checked;
  const ephemeral = !!(document.getElementById('hs-eph')||{}).checked;
  const out = document.getElementById('keyout');
  if (!user) { out.innerHTML = '<div class="keybox err">Pick a user first.</div>'; return; }
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await fetch('/api/headscale/preauthkey', {
      method:'POST', headers:{'Content-Type':'application/json'}, cache:'no-store',
      body: JSON.stringify({user, expiration, tags, reusable, ephemeral})
    });
    const d = await r.json();
    if (r.ok && d.key) {
      const cmd = 'tailscale up --login-server https://headscale.your-domain.example.com:8443 --authkey ' + d.key + ' --reset';
      out.innerHTML = `<div class=keybox>
        <b>Preauth key for ${esc(d.user)}</b> · expires ${esc(d.expiration)}${reusable?' · reusable':''}${ephemeral?' · ephemeral':''}
        <div class=keyval id=hs-keyval>${esc(d.key)}</div>
        <div class=keyval id=hs-cmd>${esc(cmd)}</div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">
          <button class=btn onclick="navigator.clipboard.writeText(document.getElementById('hs-keyval').textContent)">Copy key</button>
          <button class=btn onclick="navigator.clipboard.writeText(document.getElementById('hs-cmd').textContent)">Copy command</button>
        </div>
      </div>`;
    } else {
      out.innerHTML = `<div class="keybox err">⚠ ${esc(d.error||'failed')}${d.detail?': '+esc(d.detail):''}</div>`;
    }
  } catch (e) {
    out.innerHTML = `<div class="keybox err">⚠ ${esc(String(e))}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Create';
  }
}

function renderAWG(d) {
  const cls = d.err ? 'down' : ((d.peers||[]).length ? 'up' : 'warn');
  const rows = (d.peers||[]).map(p => `
    <tr>
      <td><span class="dot ${p.handshook?'up':'warn'}"></span></td>
      <td class=url>${esc(p.allowed_ips||'?')}</td>
      <td class=url>${esc(p.endpoint||'')||'<span class=age>(none)</span>'}</td>
      <td class=age>${p.handshook ? ago(p.last_hs_unix) : 'never'}</td>
      <td>${bytes(p.rx_bytes)} ↓ / ${bytes(p.tx_bytes)} ↑</td>
    </tr>`).join('');
  const iface = d.iface ? `Port ${esc(d.iface.listen_port||'?')} · pub ${esc((d.iface.public_key||'').slice(0,12))}…` : '';
  return `
    <div class=route>
      <h2><span class="dot ${cls}"></span>🔒 ${esc(d.label)}<span class="tag ${cls}" style="margin-left:auto">${esc(d.summary)}</span></h2>
      <div class=sub>${iface}</div>
      ${d.err ? `<div class=empty style="color:var(--neg)">⚠ ${esc(d.err)}</div>` : ''}
      ${rows ? `<table>
        <thead><tr><th></th><th>Allowed IP</th><th>Endpoint</th><th>Last handshake</th><th>Transfer</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>` : `<div class=empty>${d.note ? esc(d.note) : 'No peers configured.'}</div>`}
    </div>`;
}

function renderARK(d) {
  const upCount = (d.forwards||[]).filter(f=>f.ok).length;
  const cls = upCount === 0 ? 'warn' : (upCount === (d.forwards||[]).length ? 'up' : 'warn');
  const rows = (d.forwards||[]).map(f => `
    <tr>
      <td><span class="dot ${f.ok?'up':'down'}"></span></td>
      <td><b>${esc(f.label)}</b></td>
      <td class=url>${esc(f.proto.toUpperCase())} ${f.port}</td>
      <td>${f.ok ? 'listening' : '<span class=age>not running</span>'}</td>
    </tr>`).join('');
  return `
    <div class=route>
      <h2><span class="dot ${cls}"></span>🎮 ${esc(d.label)}<span class="tag ${cls}" style="margin-left:auto">${esc(d.summary)}</span></h2>
      <div class=sub>${esc(d.note||'')}</div>
      <table>
        <thead><tr><th></th><th>Service</th><th>Protocol/Port</th><th>Status</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function hsBanner(html, err) {
  document.getElementById('keyout').innerHTML =
    '<div class="keybox'+(err?' err':'')+'">'+html+'</div>';
}
async function hsAction(url, opts) {
  const r = await fetch(url, Object.assign({cache:'no-store'}, opts||{}));
  let d = {}; try { d = await r.json(); } catch (e) {}
  return {ok: r.ok, d};
}
function hsErr(d) { return '⚠ '+esc(d.error||'failed')+(d.detail?': '+esc(d.detail):''); }
function hsTime(t) {
  if (!t) return '—';
  if (typeof t === 'object' && t.seconds) return new Date(t.seconds*1000).toISOString().slice(0,16).replace('T',' ');
  return String(t).slice(0,16).replace('T',' ');
}

async function nodeRename(id, name) {
  const nn = prompt('New name for "'+name+'":', name);
  if (!nn) return;
  const {ok,d} = await hsAction('/api/headscale/node/'+encodeURIComponent(id)+'/rename',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:nn})});
  hsBanner(ok ? 'Renamed '+esc(name)+' → '+esc(nn) : hsErr(d), !ok); refresh();
}
async function nodeExpire(id, name) {
  if (!confirm('Expire "'+name+'"? It is logged out and must re-authenticate.')) return;
  const {ok,d} = await hsAction('/api/headscale/node/'+encodeURIComponent(id)+'/expire', {method:'POST'});
  hsBanner(ok ? 'Expired '+esc(name) : hsErr(d), !ok); refresh();
}
async function nodeDelete(id, name) {
  if (!confirm('DELETE "'+name+'" from headscale? This is permanent.')) return;
  const {ok,d} = await hsAction('/api/headscale/node/'+encodeURIComponent(id)+'/delete', {method:'POST'});
  hsBanner(ok ? 'Deleted '+esc(name) : hsErr(d), !ok); refresh();
}
async function nodeRoutes(id, name) {
  const {ok,d} = await hsAction('/api/headscale/node/'+encodeURIComponent(id)+'/routes');
  if (!ok) { hsBanner(hsErr(d), true); return; }
  const list = Array.isArray(d) ? d : (d.routes || []);
  const avail = list.map(r => r.prefix||r.route||r).filter(Boolean);
  const cur = list.filter(r => r.enabled||r.approved).map(r => r.prefix||r.route).filter(Boolean);
  const msg = 'Routes for "'+name+'"\\n'+
    'Advertised: '+(avail.join(', ')||'(none)')+'\\n'+
    'Approved now: '+(cur.join(', ')||'(none)')+'\\n\\n'+
    'Enter comma-separated CIDRs to APPROVE (blank = clear all).\\n'+
    'Exit node = 0.0.0.0/0,::/0';
  const input = prompt(msg, cur.join(','));
  if (input === null) return;
  const routes = input.split(/[\\s,]+/).filter(Boolean);
  const res = await hsAction('/api/headscale/node/'+encodeURIComponent(id)+'/routes',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({routes})});
  hsBanner(res.ok ? 'Approved for '+esc(name)+': '+(routes.join(', ')||'(cleared)') : hsErr(res.d), !res.ok);
  refresh();
}

async function renderHSControls() {
  let users = [];
  try { const r = await fetch('/api/headscale', {cache:'no-store'}); const d = await r.json();
        users = (d.users||[]).map(u => u.name).filter(Boolean); } catch (e) {}
  const uopts = users.map(u => `<option value="${esc(u)}">${esc(u)}</option>`).join('')
                || '<option value="">(no users)</option>';
  document.getElementById('hsctl').innerHTML = `
    <div class=route>
      <h2>🎛 Headscale controls</h2>
      <details class=ctl open>
        <summary>➕ Approve pending node</summary>
        <div class=sub>Sign in on the device via <b>browser</b> (not auth key) — the headscale page shows a registration key (e.g. <code>j2Ci7bW-…</code>, or older <code>nodekey:…</code>). Paste it to register without a terminal.</div>
        <div class=row>
          <select id=reg-user>${uopts}</select>
          <input id=reg-key type=text style="flex:1;min-width:160px" placeholder="j2Ci7bW-… or nodekey:…">
          <button class=btn onclick="approveNode(this)">Register</button>
        </div>
      </details>
      <details class=ctl>
        <summary>🔑 Manage preauth keys</summary>
        <div class=row>
          <select id=pk-user>${uopts}</select>
          <button class=btn onclick="loadPreauthKeys()">List keys</button>
        </div>
        <div id=pk-list></div>
      </details>
      <details class=ctl>
        <summary>📜 View ACL policy</summary>
        <div class=row><button class=btn onclick="loadACL()">Load policy</button></div>
        <pre id=acl-view class=keyval style="white-space:pre-wrap;max-height:300px;overflow:auto;display:none"></pre>
      </details>
    </div>`;
}
async function approveNode(btn) {
  const user = (document.getElementById('reg-user')||{}).value || '';
  const key = ((document.getElementById('reg-key')||{}).value || '').trim();
  if (!user || !key) { hsBanner('Pick a user and paste a node key.', true); return; }
  btn.disabled = true; btn.textContent = '…';
  const {ok,d} = await hsAction('/api/headscale/node/register',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user,key})});
  hsBanner(ok ? 'Node registered to '+esc(user)+' — it should connect shortly.' : hsErr(d), !ok);
  btn.disabled = false; btn.textContent = 'Register';
  if (ok) { document.getElementById('reg-key').value=''; refresh(); }
}
async function loadPreauthKeys() {
  const user = (document.getElementById('pk-user')||{}).value || '';
  const el = document.getElementById('pk-list');
  el.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  const {ok,d} = await hsAction('/api/headscale/preauthkeys?user='+encodeURIComponent(user));
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>'; return; }
  const keys = Array.isArray(d) ? d : [];
  if (!keys.length) { el.innerHTML = '<div class=empty>No keys for this user.</div>'; return; }
  el.innerHTML = '<table><thead><tr><th>Key</th><th>Reuse</th><th>Used</th><th>Expires</th><th></th></tr></thead><tbody>'+
    keys.map(k => {
      const kv = k.key || '';
      const id = (k.id!=null) ? String(k.id) : '';
      const exp = hsTime(k.expiration);
      return '<tr><td class=url>'+esc(String(kv).slice(0,18))+'…</td>'+
        '<td>'+(k.reusable?'yes':'no')+'</td><td>'+(k.used?'yes':'no')+'</td>'+
        '<td class=age>'+esc(exp)+'</td>'+
        '<td><button class="mini danger" onclick="expirePreauth(\\''+esc(user)+'\\',\\''+esc(id)+'\\')">expire</button></td></tr>';
    }).join('')+'</tbody></table>';
}
async function expirePreauth(user, id) {
  if (!confirm('Expire this preauth key?')) return;
  const {ok,d} = await hsAction('/api/headscale/preauthkey/expire',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user,id})});
  hsBanner(ok ? 'Preauth key expired.' : hsErr(d), !ok); loadPreauthKeys();
}
async function loadACL() {
  const el = document.getElementById('acl-view'); el.style.display='block'; el.textContent = 'Loading…';
  const {ok,d} = await hsAction('/api/headscale/acl');
  el.textContent = ok ? (d.policy || '(empty)') : hsErr(d);
}

// ── 🅐 Console: per-node detail drawer ───────────────────────────────────
async function nodeDetail(id) {
  const ov = document.getElementById('drawer-ov'), dr = document.getElementById('drawer');
  const body = document.getElementById('drawer-body');
  body.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  ov.classList.add('show'); dr.classList.add('show');
  const {ok,d} = await hsAction('/api/headscale/node/'+encodeURIComponent(id));
  body.innerHTML = ok ? renderNodeDetail(d) : '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>';
}
function closeDrawer() {
  document.getElementById('drawer-ov').classList.remove('show');
  document.getElementById('drawer').classList.remove('show');
}
function renderNodeDetail(d) {
  const routes = (d.routes||[]).map(r => esc(r.prefix||r.route||r)+((r.enabled||r.approved)?' ✅':'')).join('<br>') || '<span class=age>none</span>';
  const tags = ((d.forced_tags||[]).concat(d.valid_tags||[])).map(esc).join(', ') || '<span class=age>none</span>';
  const fmt = t => t ? esc(String(t).slice(0,19).replace('T',' ')) : '—';
  const kv = (k,v,mono) => '<div class=k>'+k+'</div><div class="v'+(mono?' mono':'')+'">'+v+'</div>';
  const id = esc(d.id), nm = esc(d.name);
  return '<h2>'+(d.online?'🟢 ':'⚪ ')+esc(d.name||'?')+'</h2>'+
    '<div class=kv>'+
      kv('Status', d.online?'<span class="sev ok">online</span>':'<span class="sev unknown">offline</span>')+
      kv('Node ID', id)+
      kv('Given name', esc(d.given_name||'—'))+
      kv('User', esc(d.user||'—'))+
      kv('Tailnet IPv4', esc(d.ipv4||'—'), true)+
      kv('Tailnet IPv6', esc(d.ipv6||'—'), true)+
      kv('Last seen', fmt(d.last_seen))+
      kv('Created', fmt(d.created_at))+
      kv('Key expiry', d.expiry?fmt(d.expiry):'never')+
      kv('Node key', esc(d.node_key||'—'), true)+
      kv('Tags', tags)+
      kv('Routes', routes)+
    '</div>'+
    '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:16px">'+
      '<button class=btn onclick="nodeRoutes(\\''+id+'\\',\\''+nm+'\\')">🧭 Routes / exit</button>'+
      '<button class=btn onclick="nodeRename(\\''+id+'\\',\\''+nm+'\\')">✎ Rename</button>'+
      '<button class=btn onclick="nodeExpire(\\''+id+'\\',\\''+nm+'\\')">⎋ Expire</button>'+
      '<button class=btn style="background:var(--neg)" onclick="nodeDelete(\\''+id+'\\',\\''+nm+'\\');closeDrawer()">🗑 Delete</button>'+
    '</div>';
}

// ── 🅐 Console: connectivity diagnostics (netcheck) ──────────────────────
async function runNetcheck(btn) {
  const el = document.getElementById('diagout');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  el.innerHTML = '<div class=empty><span class=spin></span> Running netcheck…</div>';
  try {
    const r = await fetch('/api/diag/netcheck', {cache:'no-store'});
    const d = await r.json();
    el.innerHTML = r.ok ? renderNetcheck(d) : '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>';
  } catch (e) { el.innerHTML = '<div class=empty style="color:var(--neg)">⚠ '+esc(String(e))+'</div>'; }
  if (btn) { btn.disabled = false; btn.textContent = 'Run netcheck'; }
}
function renderNetcheck(d) {
  const derp = (d.derp||[]).slice().sort((a,b)=>((a.latency_ms==null?9999:a.latency_ms)-(b.latency_ms==null?9999:b.latency_ms)));
  const max = Math.max(1, ...derp.map(x=>x.latency_ms||0));
  const rows = derp.map(x => {
    const ms = x.latency_ms;
    const w = ms ? Math.round(ms/max*100) : 0;
    const col = x.home ? 'var(--accent)' : (ms&&ms<60?'var(--pos)':(ms&&ms<150?'var(--warn)':'var(--neg)'));
    return '<tr'+(x.home?' style="background:rgba(41,151,255,0.08)"':'')+'>'+
      '<td>'+(x.home?'🏠 ':'')+esc(x.name)+' <span class=age>'+esc(x.code)+'</span></td>'+
      '<td style="width:55%"><div class=barwrap><div class=bar style="width:'+w+'%;background:'+col+'"></div></div></td>'+
      '<td class=age style="text-align:right">'+(ms!=null?ms+'ms':'—')+'</td></tr>';
  }).join('');
  return '<div class=kv style="margin-bottom:10px">'+
      '<div class=k>UDP</div><div class=v>'+(d.udp?'✅ yes':'❌ no')+'</div>'+
      '<div class=k>Public IPv4</div><div class="v mono">'+esc(d.ipv4||'—')+'</div>'+
      '<div class=k>Nearest DERP</div><div class=v>'+esc(d.nearest||'—')+'</div>'+
      '<div class=k>Checked from</div><div class=v>'+esc(d.source||'')+'</div>'+
    '</div><table><tbody>'+rows+'</tbody></table>';
}

// ── 🅐 Console: activity timeline ─────────────────────────────────────────
function evIcon(ev) {
  ev = ev||'';
  if (ev.indexOf('delete')>=0) return '🗑';
  if (ev.indexOf('acl')>=0) return '📝';
  if (ev.indexOf('dns')>=0) return '🌐';
  if (ev.indexOf('backup')>=0) return '💾';
  if (ev.indexOf('wol')>=0) return '🔌';
  if (ev.indexOf('enroll')>=0 || ev.indexOf('preauth')>=0) return '🔑';
  if (ev.indexOf('node')>=0) return '🖥';
  if (ev.indexOf('login')>=0 || ev.indexOf('auth')>=0 || ev.indexOf('redeem')>=0) return '🔐';
  return '•';
}
async function loadActivity() {
  const el = document.getElementById('activityout');
  el.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  const {ok,d} = await hsAction('/api/activity?limit=120');
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>'; return; }
  const evs = d.events||[];
  if (!evs.length) { el.innerHTML = '<div class=empty>No recorded activity yet.</div>'; return; }
  el.innerHTML = '<div class=timeline>'+evs.map(e => {
    const when = esc(String(e.ts||'').slice(0,19).replace('T',' '));
    const who = esc(e.user_id||'—');
    const ip = e.ip ? ' · '+esc(e.ip) : '';
    let pl = '';
    try { if (e.payload) pl = esc(JSON.stringify(e.payload)); } catch(x) {}
    return '<div class=tl-item><div class=tl-dot></div><div style="flex:1;min-width:0">'+
      '<div class=tl-ev>'+evIcon(e.event)+' '+esc(e.event)+'</div>'+
      '<div class=tl-meta>'+when+' · '+who+ip+'</div>'+
      (pl ? '<div class=tl-meta style="word-break:break-all">'+pl+'</div>' : '')+
    '</div></div>';
  }).join('')+'</div>';
}

// ── 🅑 Operations: add-device QR enrol modal ─────────────────────────────
function openEnroll() {
  const ov = document.getElementById('enroll-ov');
  document.getElementById('enroll-body').innerHTML =
    '<div class=fieldrow><label>Headscale user</label><input id=en-user class=txt value="azfar"></div>'+
    '<div class=fieldrow><label>Key lifetime</label><select id=en-exp class=txt>'+
      '<option value="1h">1 hour</option><option value="24h">24 hours</option><option value="10m">10 minutes</option></select></div>'+
    '<label class=cb style="margin:6px 0"><input id=en-eph type=checkbox> ephemeral (auto-removes when offline)</label>'+
    '<button class=btn style="width:100%;margin-top:8px" onclick="doEnroll(this)">Generate enrolment QR</button>'+
    '<div class=note>Generates a single-use, short-lived key. Scan the QR or copy the command on the new device.</div>'+
    '<div id=enroll-result></div>';
  ov.classList.add('show');
}
function closeEnroll() { document.getElementById('enroll-ov').classList.remove('show'); }
async function doEnroll(btn) {
  const user = (document.getElementById('en-user')||{}).value || 'azfar';
  const expiration = (document.getElementById('en-exp')||{}).value || '1h';
  const ephemeral = !!(document.getElementById('en-eph')||{}).checked;
  const res = document.getElementById('enroll-result');
  btn.disabled = true; btn.textContent = '…';
  const {ok,d} = await hsAction('/api/headscale/enroll',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user, expiration, ephemeral})});
  btn.disabled = false; btn.textContent = 'Generate enrolment QR';
  if (!ok) { res.innerHTML = '<div class="note err">'+hsErr(d)+'</div>'; return; }
  res.innerHTML = (d.qr_svg ? '<div class=qrbox>'+d.qr_svg+'</div>' : '')+
    '<div class=note>Key for <b>'+esc(d.user)+'</b> · expires in '+esc(d.expiration)+' · single-use</div>'+
    '<div class=keyval id=en-cmd>'+esc(d.cmd)+'</div>'+
    '<div style="display:flex;gap:8px;margin-top:8px">'+
      '<button class=btn onclick="navigator.clipboard.writeText(document.getElementById(\\'en-cmd\\').textContent)">Copy command</button>'+
      '<button class=btn onclick="navigator.clipboard.writeText('+JSON.stringify(d.key)+')">Copy key</button>'+
    '</div>';
}

// ── 🅑 Operations: ACL editor ─────────────────────────────────────────────
async function loadACLEditor() {
  const ta = document.getElementById('acl-editor'), note = document.getElementById('acl-note');
  note.textContent = 'Loading…'; note.className = 'note';
  const {ok,d} = await hsAction('/api/headscale/acl/raw');
  if (!ok) { note.textContent = hsErr(d); note.className='note err'; return; }
  ta.value = d.policy || '';
  ta.readOnly = !d.editable;
  note.textContent = d.editable ? ('Loaded '+(d.path||'acl.json')) : 'Read-only (config mount unavailable)';
  note.className = 'note';
}
async function checkACL() {
  const ta = document.getElementById('acl-editor'), note = document.getElementById('acl-note');
  note.textContent = 'Checking…'; note.className='note';
  const {ok,d} = await hsAction('/api/headscale/acl/check',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({policy: ta.value})});
  if (d && d.valid) { note.textContent = '✅ '+(d.detail||'Policy is valid.'); note.className='note ok'; }
  else { note.textContent = '❌ '+((d && (d.detail||d.error)) || 'invalid'); note.className='note err'; }
}
async function saveACL() {
  const ta = document.getElementById('acl-editor'), note = document.getElementById('acl-note');
  if (!confirm('Save and reload this ACL policy? The current acl.json is snapshotted first.')) return;
  note.textContent = 'Saving…'; note.className='note';
  const {ok,d} = await hsAction('/api/headscale/acl',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({policy: ta.value})});
  if (ok && d.ok) { note.textContent = '✅ Saved + reloaded'+(d.backup?(' (backup '+esc(d.backup)+')'):''); note.className='note ok'; }
  else { note.textContent = '❌ '+(d.detail||d.error||'failed'); note.className='note err'; }
}

// ── 🅒 Reliability: version / expiry / WoL / DNS / backup ─────────────────
async function loadVersion() {
  const el = document.getElementById('verout');
  el.innerHTML = '<div class=empty><span class=spin></span> Checking…</div>';
  const {ok,d} = await hsAction('/api/monitor/version');
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>'; return; }
  const sev = d.update_available ? 'warn' : 'ok';
  el.innerHTML = '<div class=kv>'+
    '<div class=k>Component</div><div class=v>'+esc(d.component)+'</div>'+
    '<div class=k>Current</div><div class=v>'+esc(d.current||'—')+'</div>'+
    '<div class=k>Latest</div><div class=v>'+esc(d.latest||'—')+' <span class="sev '+sev+'">'+(d.update_available?'update available':'up to date')+'</span></div>'+
    '</div>'+(d.latest_url?('<div class=note><a href="'+esc(d.latest_url)+'" target=_blank style="color:var(--accent)">Release notes ↗</a></div>'):'');
}
async function loadExpiry() {
  const el = document.getElementById('expout');
  el.innerHTML = '<div class=empty><span class=spin></span> Checking…</div>';
  const {ok,d} = await hsAction('/api/monitor/expiry');
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>'; return; }
  const items = d.items||[];
  if (!items.length) { el.innerHTML = '<div class=empty>Nothing tracked.</div>'; return; }
  el.innerHTML = '<table><tbody>'+items.map(it => {
    const dl = it.days_left;
    const txt = dl==null ? '—' : (dl<0?'EXPIRED':dl.toFixed(0)+'d');
    return '<tr><td>'+esc(it.name)+'<br><span class=age>'+esc(it.detail||'')+'</span></td>'+
      '<td style="text-align:right"><span class="sev '+esc(it.severity||'unknown')+'">'+txt+'</span></td></tr>';
  }).join('')+'</tbody></table>';
}
async function loadWol() {
  const el = document.getElementById('wolout');
  const {ok,d} = await hsAction('/api/wol/devices');
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>'; return; }
  const devs = d.devices||[];
  if (!devs.length) { el.innerHTML = '<div class=empty>No saved devices. Press “Add device”.</div>'; return; }
  el.innerHTML = '<table><tbody>'+devs.map(v =>
    '<tr><td><b>'+esc(v.name)+'</b><br><span class=age>'+esc(v.mac)+(v.ip?(' · '+esc(v.ip)):'')+'</span></td>'+
    '<td class=nodeact style="text-align:right">'+
      '<button class=mini title="Wake" onclick="wakeDevice(\\''+esc(v.mac)+'\\',\\''+esc(v.name)+'\\')">⏻</button>'+
      '<button class="mini danger" title="Remove" onclick="removeWolDevice(\\''+esc(v.mac)+'\\',\\''+esc(v.name)+'\\')">🗑</button>'+
    '</td></tr>').join('')+'</tbody></table>';
}
async function wakeDevice(mac, name) {
  const {ok,d} = await hsAction('/api/wol', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mac})});
  hsBanner((ok && d.ok) ? ('🔌 Magic packet sent to '+esc(name)+' ('+d.packets_sent+' pkt)') : (hsErr(d)+' '+esc(d.note||'')), !(ok&&d.ok));
}
function addWolPrompt() {
  const name = prompt('Device name:'); if (!name) return;
  const mac = prompt('MAC address (AA:BB:CC:DD:EE:FF):'); if (!mac) return;
  const ip = prompt('IP or hostname (optional):') || '';
  addWolDevice(name, mac, ip);
}
async function addWolDevice(name, mac, ip) {
  const {ok,d} = await hsAction('/api/wol/devices',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'add', name, mac, ip})});
  if (ok && d.ok) loadWol(); else hsBanner(hsErr(d), true);
}
async function removeWolDevice(mac, name) {
  if (!confirm('Remove '+name+' from the WoL list?')) return;
  const {ok,d} = await hsAction('/api/wol/devices',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'remove', mac})});
  if (ok) loadWol(); else hsBanner(hsErr(d), true);
}
async function loadDNS() {
  const el = document.getElementById('dnsout');
  const {ok,d} = await hsAction('/api/dns');
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>'; return; }
  el.innerHTML =
    '<label class=cb><input id=dns-magic type=checkbox '+(d.magic_dns?'checked':'')+'> MagicDNS</label>'+
    '<label class=cb style="margin-top:6px"><input id=dns-override type=checkbox '+(d.override_local_dns?'checked':'')+'> Override local DNS</label>'+
    '<div class=fieldrow><label>Base domain</label><input id=dns-base class=txt value="'+esc(d.base_domain||'')+'"></div>'+
    '<div class=fieldrow><label>Global nameservers (comma-separated)</label><input id=dns-ns class=txt value="'+esc((d.global_nameservers||[]).join(', '))+'"></div>'+
    '<div class=fieldrow><label>Search domains (comma-separated)</label><input id=dns-search class=txt value="'+esc((d.search_domains||[]).join(', '))+'"></div>'+
    '<div class=note>Saving rewrites the dns: block in config.yaml (backed up first), validates with configtest, then restarts headscale to apply.</div>';
}
async function saveDNS() {
  if (!confirm('Save DNS settings? Headscale restarts briefly to apply.')) return;
  const body = {
    magic_dns: !!(document.getElementById('dns-magic')||{}).checked,
    override_local_dns: !!(document.getElementById('dns-override')||{}).checked,
    base_domain: (document.getElementById('dns-base')||{}).value || 'tail.your-domain.example.com',
    global_nameservers: (((document.getElementById('dns-ns')||{}).value)||'').split(/[\\s,]+/).filter(Boolean),
    search_domains: (((document.getElementById('dns-search')||{}).value)||'').split(/[\\s,]+/).filter(Boolean)
  };
  const {ok,d} = await hsAction('/api/dns', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  hsBanner((ok && d.ok) ? ('🌐 DNS saved'+(d.restarted?' + headscale restarted':' (restart pending)')) : ((d.rolled_back?'Rolled back: ':'')+hsErr(d)), !(ok&&d.ok));
}
async function loadBackups() {
  const el = document.getElementById('backupout');
  const {ok,d} = await hsAction('/api/backup/list');
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">'+hsErr(d)+'</div>'; return; }
  const bks = d.backups||[];
  if (!bks.length) { el.innerHTML = '<div class=empty>No snapshots yet.</div>'; return; }
  el.innerHTML = '<table><tbody>'+bks.map(b =>
    '<tr><td><b>'+esc(b.name)+'</b><br><span class=age>'+esc(String(b.created||'').slice(0,19).replace('T',' '))+' · '+bytes(b.size)+' · '+(b.files||[]).length+' files</span></td>'+
    '<td class=nodeact style="text-align:right">'+
      '<button class=mini title="Download" onclick="downloadBackup(\\''+esc(b.name)+'\\')">⬇</button>'+
      '<button class=mini title="Restore" onclick="restoreBackup(\\''+esc(b.name)+'\\')">♻</button>'+
    '</td></tr>').join('')+'</tbody></table>';
}
async function createBackup(btn) {
  btn.disabled = true; btn.textContent = '…';
  const {ok,d} = await hsAction('/api/backup/create', {method:'POST'});
  btn.disabled = false; btn.textContent = 'Snapshot now';
  hsBanner((ok && d.ok) ? ('💾 Snapshot '+esc(d.name)+' created') : hsErr(d), !(ok&&d.ok));
  if (ok && d.ok) loadBackups();
}
function downloadBackup(name) { window.open('/api/backup/'+encodeURIComponent(name)+'/download', '_blank'); }
async function restoreBackup(name) {
  if (!confirm('RESTORE '+name+'? Headscale stops, the current DB is snapshotted, then this backup is swapped in and headscale restarts. Nodes briefly disconnect.')) return;
  hsBanner('Restoring '+esc(name)+'… headscale is restarting.', false);
  const {ok,d} = await hsAction('/api/backup/restore',
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  hsBanner((ok && d.ok) ? ('♻ Restored '+esc(name)+' (pre-restore snapshot: '+esc(d.pre_restore_backup||'?')+')') : ('❌ '+hsErr(d)), !(ok&&d.ok));
  loadBackups(); setTimeout(refresh, 4000);
}

// ── Discovery: LAN scan + tailnet, tap-to-copy ───────────────────────────
function copyChip(val, label) {
  if (!val) return '<span class=age>—</span>';
  return `<span class=copychip data-copy="${esc(val)}" onclick="copyVal(this)" title="Tap to copy">${esc(label || val)}</span>`;
}
function copyVal(el) {
  const v = el.getAttribute('data-copy');
  try {
    navigator.clipboard.writeText(v);
    const t = el.textContent; el.textContent = '✓ copied'; el.classList.add('copied');
    setTimeout(() => { el.textContent = t; el.classList.remove('copied'); }, 900);
  } catch (e) {}
}
async function scanLan(btn) {
  const el = document.getElementById('lanout');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  el.innerHTML = '<div class=empty><span class=spin></span> Ping-sweeping the LAN…</div>';
  const {ok, d} = await hsAction('/api/discover');
  if (btn) { btn.disabled = false; btn.textContent = 'Scan LAN'; }
  if (!ok || !d || d.error) { el.innerHTML = '<div class=empty style="color:var(--neg)">' + hsErr(d || {}) + '</div>'; return; }
  const devs = d.devices || [];
  if (!devs.length) { el.innerHTML = '<div class=empty>No devices found (anything off or blocking ping won’t show).</div>'; return; }
  el.innerHTML = '<table><thead><tr><th>Device</th><th>IP</th><th>MAC</th></tr></thead><tbody>' +
    devs.map(v => '<tr><td>' + (v.hostname ? esc(v.hostname) : '<span class=age>—</span>') + '</td>' +
      '<td>' + copyChip(v.ip) + '</td><td>' + copyChip(v.mac) + '</td></tr>').join('') +
    '</tbody></table><div class=note>' + devs.length + ' device(s) · tap an IP or MAC to copy' + (d.cached ? ' · cached' : '') + '</div>';
}
async function loadDiscoveryTailnet() {
  const el = document.getElementById('tnout');
  const {ok, d} = await hsAction('/api/headscale');
  if (!ok) { el.innerHTML = '<div class=empty style="color:var(--neg)">' + hsErr(d) + '</div>'; return; }
  const nodes = d.nodes || [];
  if (!nodes.length) { el.innerHTML = '<div class=empty>No tailnet nodes.</div>'; return; }
  el.innerHTML = '<table><thead><tr><th></th><th>Node</th><th>Tailnet IP</th></tr></thead><tbody>' +
    nodes.map(n => '<tr><td><span class="dot ' + (n.online ? 'up' : 'down') + '"></span></td>' +
      '<td><b>' + esc(n.name) + '</b></td><td>' + copyChip(n.ipv4 || '') + '</td></tr>').join('') +
    '</tbody></table>';
}

// ── tab switching + lazy panel loading ───────────────────────────────────
const _tabLoaded = {};
async function loadPorts(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  const el = document.getElementById('portsout');
  try {
    const r = await fetch('/api/ports', {cache:'no-store'});
    const d = await r.json();
    el.innerHTML = renderPorts(d);
  } catch (e) {
    el.innerHTML = '<div class=empty>Failed to load ports: ' + esc(String(e)) + '</div>';
  }
  if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
}
function renderPorts(d) {
  if (d && d.error) return '<div class=empty>' + esc(d.error) + (d.detail ? ': ' + esc(d.detail) : '') + '</div>';
  const ports = (d && d.ports) || [];
  if (!ports.length) return '<div class=empty>No listening ports reported.</div>';
  const rows = ports.map(p =>
    `<tr><td class=url>${esc(p.proto)}</td><td>${esc(String(p.port))}</td><td class=url>${esc(p.address||'')}</td>`
    + `<td>${esc(p.process||'')}${p.pid ? ' <span class=age>('+p.pid+')</span>' : ''}</td></tr>`).join('');
  return `<table><thead><tr><th>Proto</th><th>Port</th><th>Address</th><th>Process</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ── Two-level nav: left = service, top = the service's views (mirrors the
// desktop app's nav.ts IA). Content units carry data-svc/data-sub; nav() shows
// the matching ones, renders the sub-nav, and lazy-loads on first visit.
const SVC = [
  {id:'overview', l:'Overview', s:'at a glance', subs:[]},
  {id:'tailnet', l:'Tailnet', s:'headscale', subs:[['nodes','Nodes'],['keys','Keys'],['routes','Routes & Exit'],['acl','ACL'],['dns','DNS']]},
  {id:'amneziawg', l:'AmneziaWG', s:'obfuscated VPN', subs:[['peers','Peers']]},
  {id:'router', l:'Router', s:'AsusWRT', subs:[['forwards','Port Forwards'],['upnp','UPnP']]},
  {id:'devices', l:'Devices', s:'LAN', subs:[['discovery','Discovery'],['wol','Wake-on-LAN']]},
  {id:'diagnostics', l:'Diagnostics', s:'reachability', subs:[['netcheck','Netcheck'],['cf','CF Tunnels'],['edge','Edge']]},
  {id:'local', l:'Local', s:'host', subs:[['ports','Ports'],['forwarder','Forwarder']]},
  {id:'maintenance', l:'Maintenance', s:'ops', subs:[['activity','Activity'],['backup','Backup'],['version','Version']]},
];
const _navLoaded = {};
let CUR_SVC = 'overview', CUR_SUB = '';
function _defaultSub(svc) { const x = SVC.find(s => s.id === svc); return x && x.subs[0] ? x.subs[0][0] : ''; }
function renderSvcNav() {
  document.getElementById('svcnav').innerHTML = SVC.map(s =>
    `<button class="svcbtn${s.id === CUR_SVC ? ' active' : ''}" onclick="nav('${s.id}')"><span class=l>${s.l}</span><span class=s>${s.s}</span></button>`).join('');
}
function nav(svc, sub) {
  CUR_SVC = svc;
  CUR_SUB = (sub === undefined || sub === null) ? _defaultSub(svc) : sub;
  const svcObj = SVC.find(s => s.id === svc);
  const subs = svcObj ? svcObj.subs : [];
  renderSvcNav();
  const sn = document.getElementById('subnav');
  sn.innerHTML = subs.map(([id, label]) =>
    `<button class="subbtn${id === CUR_SUB ? ' active' : ''}" onclick="nav('${svc}','${id}')">${label}</button>`).join('');
  sn.style.display = subs.length ? 'flex' : 'none';
  document.querySelectorAll('[data-svc]').forEach(el => {
    const okSvc = el.dataset.svc === svc;
    const elSubs = (el.dataset.sub || '').split(' ').filter(Boolean);
    const okSub = !CUR_SUB || elSubs.length === 0 || elSubs.includes(CUR_SUB);
    el.classList.toggle('hide', !(okSvc && okSub));
  });
  lazyNav(svc, CUR_SUB);
}
function lazyNav(svc, sub) {
  const key = svc + '/' + sub;
  if (_navLoaded[key]) return; _navLoaded[key] = true;
  if (svc === 'maintenance' && sub === 'activity') loadActivity();
  else if (svc === 'tailnet' && sub === 'acl') loadACLEditor();
  else if (svc === 'devices' && sub === 'discovery') loadDiscoveryTailnet();
  else if (svc === 'tailnet' && sub === 'dns') loadDNS();
  else if (svc === 'devices' && sub === 'wol') loadWol();
  else if (svc === 'maintenance' && sub === 'version') { loadVersion(); loadExpiry(); }
  else if (svc === 'maintenance' && sub === 'backup') loadBackups();
  else if (svc === 'local' && sub === 'ports') loadPorts();
}
// Back-compat shim for the right-rail's switchTab() calls.
function switchTab(name) {
  const map = {tailnet:['tailnet','nodes'], diag:['diagnostics','netcheck'], maint:['maintenance','version'],
               discovery:['devices','discovery'], activity:['maintenance','activity'], access:['tailnet','keys'], overview:['overview']};
  const t = map[name] || [name]; nav(t[0], t[1]);
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeDrawer(); closeEnroll(); } });

async function refresh() {
  try {
    const r = await fetch('/api/all', {cache:'no-store'});
    const d = await r.json();
    // Overview = CF tunnels + AmneziaWG + Ark forwards; Tailnet = the headscale nodes.
    document.getElementById('routes').innerHTML = renderCF(d.cf) + renderARK(d.ark);
    const _awg = document.getElementById('awgout'); if (_awg) _awg.innerHTML = renderAWG(d.amneziawg);
    document.getElementById('hsnodes').innerHTML = renderHS(d.headscale);
    document.getElementById('last-refresh').textContent =
      'Refreshed ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('last-refresh').textContent = 'Refresh failed: ' + e;
  }
}

refresh();
renderHSControls();
nav('overview');
setInterval(refresh, 30000);
</script>
</body></html>"""


@app.get("/network", response_class=HTMLResponse)
async def network_dashboard(request: Request):
    """Sentinel Network dashboard — VPN routes, Headscale, AmneziaWG, WoL.
    On the dedicated SURFACE=network instance this is also the bare `/`."""
    if not _is_authed(request):
        return RedirectResponse(url="/?next=/network", status_code=302)
    return HTMLResponse(HTML)


@app.get("/vpn", response_class=HTMLResponse)
async def vpn_dashboard(request: Request):
    """Legacy alias — Sentinel Network used to be branded 'VPN' and lived at
    `/vpn`. Kept as a redirect so old links/bookmarks keep working."""
    return RedirectResponse(url="/network", status_code=301)


# ── Sentinel Suite launcher (PWA + TWA-wrappable) ────────────────────────────
# This service hosts the Suite launcher at `/` (so the TWA can target the
# bare domain) and the VPN dashboard at `/vpn`.

SUITE_HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Sentinel Suite</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#1c1c1e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>
:root { --bg:#1c1c1e; --fg:#e8e8ea; --muted:#8e8e93; --section:#2c2c2e;
        --sep:#38383a; --accent:#2997ff; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { background:var(--bg); color:var(--fg); margin:0; padding:0; min-height:100vh; }
body { font:15px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       padding: env(safe-area-inset-top) 14px env(safe-area-inset-bottom);
       display:flex; flex-direction:column; }
header { display:flex; align-items:center; gap:10px; padding:18px 4px 22px; }
header .logo { width:36px; height:36px; border-radius:8px; background:linear-gradient(135deg,#2997ff,#34c759); display:flex; align-items:center; justify-content:center; font-size:20px; }
header h1 { margin:0; font-size:19px; font-weight:700; }
header .sub { font-size:11px; color:var(--muted); margin-top:2px; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; max-width:560px; margin: 0 auto 24px; width:100%; }
.tile { background:var(--section); border:1px solid var(--sep); border-radius:18px;
        padding:18px 16px; text-decoration:none; color:var(--fg);
        display:flex; flex-direction:column; gap:6px; min-height:128px;
        transition: transform 0.1s, background 0.15s; }
.tile:active { transform: scale(0.97); }
.tile:hover { background: rgba(255,255,255,0.04); }
.tile .ico { font-size:30px; line-height:1; margin-bottom:6px; }
.tile .name { font-size:15px; font-weight:600; }
.tile .desc { font-size:11px; color:var(--muted); line-height:1.35; }
.tile.accent { background:linear-gradient(135deg, rgba(41,151,255,0.18), rgba(52,199,89,0.10)); }
.tile { position:relative; }
.tile .dot {
  position:absolute; top:14px; right:14px; width:8px; height:8px;
  border-radius:50%; background:#5a5a5a; transition: background .2s;
  box-shadow: 0 0 0 2px rgba(28,28,30,.8);
}
.tile .dot.ok   { background:#34c759; }
.tile .dot.down { background:#ff453a; }
footer { color:var(--muted); font-size:10px; text-align:center; padding:8px 0 20px; margin-top:auto; }
.bell { margin-left:auto; position:relative; width:40px; height:40px; border-radius:50%;
        background:var(--section); border:1px solid var(--sep); color:var(--fg);
        font-size:19px; display:flex; align-items:center; justify-content:center;
        cursor:pointer; -webkit-tap-highlight-color:transparent; }
.bell:active { transform:scale(0.94); }
.bell .badge { position:absolute; top:-4px; right:-4px; min-width:18px; height:18px;
        padding:0 5px; border-radius:9px; background:#ff453a; color:#fff;
        font-size:11px; font-weight:700; line-height:18px; text-align:center;
        display:none; box-shadow:0 0 0 2px var(--bg); }
.bell .badge.show { display:block; }
.notif-overlay { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:50;
        display:none; }
.notif-overlay.show { display:block; }
.notif-panel { position:fixed; top:0; right:0; bottom:0; width:min(420px,92vw);
        background:var(--bg); border-left:1px solid var(--sep); z-index:51;
        transform:translateX(100%); transition:transform .22s ease;
        display:flex; flex-direction:column;
        padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }
.notif-panel.show { transform:translateX(0); }
.notif-head { display:flex; align-items:center; gap:10px; padding:18px 16px 12px;
        border-bottom:1px solid var(--sep); }
.notif-head h2 { margin:0; font-size:17px; font-weight:700; flex:1; }
.notif-head button { background:none; border:none; color:var(--accent);
        font-size:14px; cursor:pointer; padding:4px 6px; }
.notif-head .close { color:var(--muted); font-size:22px; }
.notif-list { flex:1; overflow-y:auto; padding:6px 0 16px; }
.notif-item { display:flex; gap:12px; padding:14px 16px; border-bottom:1px solid var(--sep); }
.notif-item.unread { background:rgba(41,151,255,0.07); }
.notif-item .ic { font-size:20px; line-height:1.2; }
.notif-item .title { font-weight:600; font-size:14px; }
.notif-item .body { font-size:13px; color:var(--fg); margin-top:2px; white-space:pre-wrap; word-break:break-word; }
.notif-item .age { font-size:11px; color:var(--muted); margin-top:4px; }
.notif-empty { color:var(--muted); text-align:center; padding:40px 16px; font-size:13px; }
.notif-settings { display:none; padding:8px 16px 14px; border-bottom:1px solid var(--sep);
        background:rgba(127,127,127,0.05); }
.notif-settings.show { display:block; }
.ns-head { font-size:11px; text-transform:uppercase; letter-spacing:.04em;
        color:var(--muted); margin:8px 0 4px; }
.ns-row { display:flex; align-items:center; gap:12px; padding:9px 2px; }
.ns-row .lab { flex:1; font-size:14px; }
.ns-row .hint { font-size:11px; color:var(--muted); }
.ns-sub { padding-left:14px; }
.sw { position:relative; display:inline-block; width:42px; height:24px; flex:none; }
.sw input { opacity:0; width:0; height:0; }
.sw .sl { position:absolute; inset:0; cursor:pointer; background:#555; border-radius:24px;
        transition:.18s; }
.sw .sl:before { content:''; position:absolute; height:18px; width:18px; left:3px; top:3px;
        background:#fff; border-radius:50%; transition:.18s; }
.sw input:checked + .sl { background:var(--accent); }
.sw input:checked + .sl:before { transform:translateX(18px); }
.sw input:disabled + .sl { opacity:.45; cursor:default; }
</style></head><body>

<header>
  <div class=logo>🛡</div>
  <div>
    <h1>Sentinel Suite</h1>
    <div class=sub>Owner-only · your-domain.example.com</div>
  </div>
  <button class=bell id=notif-bell aria-label=Notifications>🔔<span class=badge id=notif-badge></span></button>
</header>

<div class=notif-overlay id=notif-overlay></div>
<aside class=notif-panel id=notif-panel>
  <div class=notif-head>
    <h2>Notifications</h2>
    <button id=notif-settings-btn aria-label=Settings>⚙</button>
    <button id=notif-readall>Mark all read</button>
    <button class=close id=notif-close aria-label=Close>×</button>
  </div>
  <div class=notif-settings id=notif-settings>
    <div class=ns-head>End-of-turn ping</div>
    <div class=ns-row>
      <div class=lab>Ping when Claude finishes a turn<div class=hint>Master switch for idle pings</div></div>
      <label class=sw><input type=checkbox data-pref=idle_ping><span class=sl></span></label>
    </div>
    <div class=ns-head>Deliver via</div>
    <div class=ns-row ns-sub>
      <div class=lab>In-app feed<div class=hint>This bell + the agent dashboard</div></div>
      <label class=sw><input type=checkbox data-pref=ch_app><span class=sl></span></label>
    </div>
    <div class=ns-row ns-sub>
      <div class=lab>Web push<div class=hint>OS lock-screen notification</div></div>
      <label class=sw><input type=checkbox data-pref=ch_push><span class=sl></span></label>
    </div>
    <div class=ns-row ns-sub>
      <div class=lab>Telegram<div class=hint>@Sentinel_claude_testbot_bot</div></div>
      <label class=sw><input type=checkbox data-pref=ch_telegram><span class=sl></span></label>
    </div>
    <div class=ns-row><button id=notif-push>Enable push</button></div>
  </div>
  <div class=notif-list id=notif-list></div>
</aside>

<div class=grid>
  <a class=tile data-host="sentinelfinance.your-domain.example.com" href="https://sentinelfinance.your-domain.example.com/">
    <span class=dot></span>
    <div class=ico>💰</div>
    <div class=name>Finance</div>
    <div class=desc>Net worth, statements, reconciliation</div>
  </a>
  <a class=tile data-host="media.your-domain.example.com" href="https://media.your-domain.example.com/app">
    <span class=dot></span>
    <div class=ico>📥</div>
    <div class=name>Media</div>
    <div class=desc>SMDL · downloads, scraper, live</div>
  </a>
  <a class=tile data-host="your-domain.example.com" href="https://your-domain.example.com/">
    <span class=dot></span>
    <div class=ico>🤖</div>
    <div class=name>Sentinel AI</div>
    <div class=desc>Agent, chat, memory</div>
  </a>
  <a class="tile accent" href="/chat">
    <span class="dot ok"></span>
    <div class=ico>💬</div>
    <div class=name>Chat</div>
    <div class=desc>Shared-brain chat · agent + tools</div>
  </a>
  <a class=tile data-host="sentinelgaming.your-domain.example.com" href="https://sentinelgaming.your-domain.example.com/games">
    <span class=dot></span>
    <div class=ico>🎮</div>
    <div class=name>Gaming</div>
    <div class=desc>ARK · Game Servers · (VPN soon)</div>
  </a>
  <a class="tile accent" data-host="network.your-domain.example.com" href="https://network.your-domain.example.com/">
    <span class="dot ok"></span>
    <div class=ico>🌐</div>
    <div class=name>Network</div>
    <div class=desc>VPN · Headscale · AmneziaWG · WoL · subnet routes</div>
  </a>
  <a class=tile data-host="watchdog.your-domain.example.com" href="https://watchdog.your-domain.example.com/miniapp">
    <span class=dot></span>
    <div class=ico>🛡️</div>
    <div class=name>Watchdog</div>
    <div class=desc>Service health · probes · restart · ops</div>
  </a>
  <a class=tile href="/apps">
    <span class="dot ok"></span>
    <div class=ico>📦</div>
    <div class=name>Apps</div>
    <div class=desc>Sideload installers · self-hosted Play Store</div>
  </a>
__LICENSES_TILE__
__USERS_TILE__
</div>

<script>
// Light-touch health dots — pull the suite's own /api/cf aggregator
// (one HEAD per tunneled hostname every ~30s) and paint a coloured dot
// per tile. Doesn't gate the click — even a "down" tile remains tappable
// in case you want to investigate. Failure to fetch /api/cf just leaves
// the dots grey (unknown) — never blocks the launcher.
async function refreshDots() {
  try {
    const r = await fetch('/api/cf', { credentials: 'same-origin', cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    const map = {};
    for (const route of (data.routes || [])) {
      map[route.hostname] = route.ok;
    }
    document.querySelectorAll('.tile[data-host]').forEach(t => {
      const host = t.getAttribute('data-host');
      const dot  = t.querySelector('.dot');
      if (!dot) return;
      if (host in map) dot.classList.toggle('ok', !!map[host]);
      if (host in map) dot.classList.toggle('down', !map[host]);
    });
  } catch (_) { /* leave dots grey on transient failures */ }
}
refreshDots();
setInterval(refreshDots, 30_000);

// ── Notifications (shared feed, proxied to the agent dashboard bridge) ───────
const NOTIF_ICONS = { info:'ℹ️', success:'✅', warning:'⚠️', error:'🚨' };
function notifAge(epochSec) {
  const s = Math.max(0, Math.floor(Date.now()/1000 - (epochSec||0)));
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}
function esc(t) { const d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; }
function paintBadge(n) {
  const b = document.getElementById('notif-badge');
  if (n > 0) { b.textContent = n > 99 ? '99+' : n; b.classList.add('show'); }
  else { b.classList.remove('show'); }
}
async function loadNotifs(render) {
  try {
    const r = await fetch('/api/notifications', { credentials:'same-origin', cache:'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    paintBadge(data.unread || 0);
    if (!render) return;
    const list = document.getElementById('notif-list');
    const items = data.notifications || [];
    if (!items.length) { list.innerHTML = '<div class=notif-empty>No notifications yet.</div>'; return; }
    list.innerHTML = items.map(n =>
      '<div class="notif-item' + (n.read_at ? '' : ' unread') + '" data-id="' + n.id + '">' +
        '<div class=ic>' + (NOTIF_ICONS[n.level] || '🔔') + '</div>' +
        '<div style="flex:1">' +
          (n.title ? '<div class=title>' + esc(n.title) + '</div>' : '') +
          (n.body ? '<div class=body>' + esc(n.body) + '</div>' : '') +
          '<div class=age>' + notifAge(n.created_at) + (n.source ? ' · ' + esc(n.source) : '') + '</div>' +
        '</div>' +
      '</div>'
    ).join('');
  } catch (_) { /* leave UI as-is on transient failure */ }
}
function openNotifs() {
  document.getElementById('notif-overlay').classList.add('show');
  document.getElementById('notif-panel').classList.add('show');
  loadNotifs(true);
}
function closeNotifs() {
  document.getElementById('notif-overlay').classList.remove('show');
  document.getElementById('notif-panel').classList.remove('show');
}
document.getElementById('notif-bell').addEventListener('click', openNotifs);
document.getElementById('notif-close').addEventListener('click', closeNotifs);
document.getElementById('notif-overlay').addEventListener('click', closeNotifs);
document.getElementById('notif-readall').addEventListener('click', async () => {
  try { await fetch('/api/notifications/read-all', { method:'POST', credentials:'same-origin' }); } catch (_) {}
  loadNotifs(true);
});
document.getElementById('notif-list').addEventListener('click', async (e) => {
  const item = e.target.closest('.notif-item');
  if (!item || !item.classList.contains('unread')) return;
  const id = item.getAttribute('data-id');
  item.classList.remove('unread');
  try { await fetch('/api/notifications/' + id + '/read', { method:'POST', credentials:'same-origin' }); } catch (_) {}
  loadNotifs(false);
});
loadNotifs(false);
setInterval(() => loadNotifs(false), 30_000);

// ── Notification preferences (toggles proxied to the bridge prefs store) ─────
const PREF_KEYS = ['idle_ping', 'ch_app', 'ch_push', 'ch_telegram'];
function prefInput(key) { return document.querySelector('input[data-pref="' + key + '"]'); }
async function loadPrefs() {
  try {
    const r = await fetch('/api/notify/prefs', { credentials:'same-origin', cache:'no-store' });
    if (!r.ok) return;
    const { prefs } = await r.json();
    if (!prefs) return;
    PREF_KEYS.forEach(k => { const el = prefInput(k); if (el) el.checked = !!prefs[k]; });
  } catch (_) { /* leave toggles as-is on transient failure */ }
}
async function savePref(key, val) {
  try {
    await fetch('/api/notify/prefs', {
      method:'POST', credentials:'same-origin',
      headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ [key]: val })
    });
  } catch (_) { /* best-effort; reload reflects server truth */ }
}
PREF_KEYS.forEach(k => {
  const el = prefInput(k);
  if (el) el.addEventListener('change', () => savePref(k, el.checked));
});
document.getElementById('notif-settings-btn').addEventListener('click', () => {
  const s = document.getElementById('notif-settings');
  s.classList.toggle('show');
  if (s.classList.contains('show')) loadPrefs();
});

// ── Native web push (service worker + VAPID subscribe) ───────────────────────
const pushBtn = document.getElementById('notif-push');
let swReg = null;
function urlB64ToUint8Array(b64) {
  const pad = '='.repeat((4 - b64.length % 4) % 4);
  const base64 = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
function pushSupported() {
  return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
}
async function reflectPushState() {
  if (!pushSupported()) { pushBtn.textContent = 'Push N/A'; pushBtn.disabled = true; return; }
  if (Notification.permission === 'denied') { pushBtn.textContent = 'Push blocked'; pushBtn.disabled = true; return; }
  try {
    const sub = swReg && await swReg.pushManager.getSubscription();
    pushBtn.textContent = sub ? 'Disable push' : 'Enable push';
  } catch (_) { pushBtn.textContent = 'Enable push'; }
}
async function enablePush() {
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') { reflectPushState(); return; }
  const r = await fetch('/api/push/vapid-public', { credentials:'same-origin', cache:'no-store' });
  const { publicKey, enabled } = await r.json();
  if (!enabled || !publicKey) { pushBtn.textContent = 'Push unavailable'; return; }
  const sub = await swReg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlB64ToUint8Array(publicKey)
  });
  await fetch('/api/push/subscribe', {
    method:'POST', credentials:'same-origin',
    headers:{ 'Content-Type':'application/json' },
    body: JSON.stringify(sub.toJSON())
  });
  reflectPushState();
}
async function disablePush() {
  try {
    const sub = await swReg.pushManager.getSubscription();
    if (sub) {
      await fetch('/api/push/unsubscribe', {
        method:'POST', credentials:'same-origin',
        headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify({ endpoint: sub.endpoint })
      });
      await sub.unsubscribe();
    }
  } catch (_) {}
  reflectPushState();
}
pushBtn.addEventListener('click', async () => {
  if (!swReg) return;
  const sub = await swReg.pushManager.getSubscription();
  if (sub) disablePush(); else enablePush();
});
if (pushSupported()) {
  navigator.serviceWorker.register('/sw.js').then(reg => { swReg = reg; reflectPushState(); })
    .catch(() => { pushBtn.textContent = 'Push N/A'; pushBtn.disabled = true; });
} else {
  pushBtn.textContent = 'Push N/A'; pushBtn.disabled = true;
}
</script>

<footer>Tap a tile to launch · By Azfar · Powered by Claude</footer>

</body></html>"""


# Minimal web app manifest — bubblewrap reads this at TWA-init time. The
# network surface gets its own identity so the installed app is "Sentinel
# Network", distinct from the Suite launcher TWA.
if SURFACE == "network":
    WEB_MANIFEST = {
        "name":        "Sentinel Network",
        "short_name":  "Network",
        "description": "Owner-only tailnet control plane — headscale nodes, keys, routes.",
    }
else:
    WEB_MANIFEST = {
        "name":        "Sentinel Suite",
        "short_name":  "Sentinel",
        "description": "Owner-only launcher for Sentinel Finance, SMDL Media, Sentinel AI, and Network ops.",
    }
WEB_MANIFEST.update({
    "start_url":        "/",
    "scope":            "/",
    "display":          "standalone",
    "orientation":      "portrait",
    "theme_color":      "#1c1c1e",
    "background_color": "#1c1c1e",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
})


SETUP_HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Sentinel Suite · Setup</title>
<meta name="theme-color" content="#1c1c1e">
<style>
* { box-sizing:border-box; }
html,body { background:#1c1c1e; color:#e8e8ea; margin:0; min-height:100vh;
            font:15px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width:380px; margin:0 auto; padding:60px 22px; text-align:center; }
.logo { font-size:48px; margin-bottom:8px; }
h1 { margin:0 0 6px; font-size:22px; }
.sub { color:#8e8e93; font-size:13px; margin-bottom:32px; }
label { display:block; color:#8e8e93; font-size:11px; text-align:left;
        text-transform:uppercase; letter-spacing:0.06em; margin-bottom:6px; }
input[type=password] { width:100%; padding:14px; border-radius:12px; border:1px solid #38383a;
                       background:#2c2c2e; color:#e8e8ea; font:15px monospace;
                       outline:none; -webkit-appearance:none; }
input[type=password]:focus { border-color:#2997ff; }
button { width:100%; margin-top:18px; padding:14px; border:none; border-radius:12px;
         background:#2997ff; color:white; font-size:15px; font-weight:600; cursor:pointer; }
button:active { background:#0a84ff; }
.err { color:#ff453a; font-size:12px; margin-top:14px; min-height:18px; }
.hint { color:#636366; font-size:11px; margin-top:32px; line-height:1.5; }
</style></head><body>
<div class=wrap>
  <div class=logo>🛡</div>
  <h1>Sentinel Suite</h1>
  <div class=sub>One-time setup · paste your owner token</div>

  <form method=POST action="/auth/setup">
    <label for=token>Owner token</label>
    <input id=token name=token type=password autocomplete=off autofocus
           placeholder="64-char hex">
    <input type=hidden name=next value="__NEXT__">
    <button type=submit>Activate</button>
    <div class=err>__ERR__</div>
  </form>

  <div class=hint>Token lives in <code>OWNER_AUTH_TOKEN</code> on the host (<code>.env.local</code>).
  Cookie persists 90 days on <code>.your-domain.example.com</code> — covers all 4 tiles.</div>
</div>
</body></html>"""


def _set_session_cookie(resp, request: Request):
    """Set the long-lived owner session cookie. Domain=.your-domain.example.com when the
    request came in on a public host; otherwise host-only (loopback dev)."""
    host = (request.url.hostname or "").lower()
    domain = COOKIE_DOMAIN if host.endswith("your-domain.example.com") else None
    resp.set_cookie(
        key=COOKIE_NAME,
        value=_issue_cookie(),
        max_age=COOKIE_TTL_SEC,
        domain=domain,
        path="/",
        secure=domain is not None,
        httponly=True,
        samesite="lax",
    )


USERS_TILE_HTML = """  <a class=tile href="/admin/users">
    <span class="dot ok"></span>
    <div class=ico>🔑</div>
    <div class=name>Users</div>
    <div class=desc>__USERS_DESC__</div>
  </a>"""

LICENSES_TILE_HTML = """  <a class=tile href="/admin/licenses">
    <span class="dot ok"></span>
    <div class=ico>📜</div>
    <div class=name>Licenses</div>
    <div class=desc>View + revoke issued keys</div>
  </a>"""


def _render_suite(request: Request) -> str:
    """Render the launcher with the owner-only Users tile spliced in when
    the authenticated user is the owner. Beta users get the page without
    it. Failure to read users_db falls back to a generic label."""
    page = SUITE_HTML
    if _is_owner(request):
        try:
            n_users = len(users_db.list_users())
            n_pending = users_db.count_pending_invites()
            desc = f"{n_users} beta · {n_pending} invite{'' if n_pending == 1 else 's'} pending"
        except Exception:
            desc = "Manage beta users + scopes"
        tile = USERS_TILE_HTML.replace("__USERS_DESC__", desc)
    else:
        tile = ""
    # Licenses tile for the owner or any beta user holding licenses.manage.
    payload = _get_payload(request)
    lic_tile = LICENSES_TILE_HTML if (payload and auth_v2.has_scope(payload, "licenses.manage")) else ""
    return (page.replace("__USERS_TILE__", tile)
                .replace("__LICENSES_TILE__", lic_tile))


@app.get("/", response_class=HTMLResponse)
async def suite(request: Request, next: str = "/"):
    """Sentinel Suite launcher (4 tiles). Served at `/` so the TWA can target
    the bare domain (`https://suite.your-domain.example.com/`). VPN dashboard lives
    at `/vpn`. Unauthenticated requests get the setup form."""
    if not _is_authed(request):
        page = SETUP_HTML.replace("__NEXT__", next).replace("__ERR__", "")
        return HTMLResponse(page)
    if SURFACE == "network":
        return HTMLResponse(HTML)
    return HTMLResponse(_render_suite(request))


def _safe_token_eq(a: str, b: str) -> bool:
    """Constant-time token comparison that tolerates pasted unicode.

    `hmac.compare_digest` refuses string inputs containing non-ASCII bytes
    (raises TypeError → 500). Mobile keyboards routinely sneak in smart
    quotes, NBSPs, or trailing zero-width chars. We encode to bytes (which
    compare_digest accepts unconditionally) and pre-trim ASCII-printables only
    so a stray autocorrect glyph just produces a clean 'invalid token' page
    instead of an opaque 500."""
    if not a or not b:
        return False
    try:
        a_clean = "".join(ch for ch in a if 32 <= ord(ch) < 127)
        return hmac.compare_digest(a_clean.encode("utf-8"), b.encode("utf-8"))
    except Exception:
        return False


@app.post("/auth/setup")
async def auth_setup(request: Request):
    """Validate owner token → set domain-wide session cookie → 302 to next.
    Same endpoint exists on sentinel-miniapp-v2 and sentinel-smdl. With the
    domain-wide cookie set here, those subdomains read the cookie too — so
    a single setup hop authorises all 4 tiles."""
    try:
        form = await request.form()
    except Exception:
        page = SETUP_HTML.replace("__NEXT__", "/").replace("__ERR__", "Malformed request")
        return HTMLResponse(page, status_code=400)
    token = (form.get("token") or "").strip()
    nxt   = (form.get("next") or "/").strip()
    if not nxt.startswith("/"):
        nxt = "/"
    if not _safe_token_eq(token, OWNER_AUTH_TOKEN):
        try:
            users_db.log_event("access.denied", ip=_client_ip(request),
                               user_agent=request.headers.get("user-agent"),
                               payload={"route": "/auth/setup", "reason": "bad token"})
        except Exception:
            pass
        page = SETUP_HTML.replace("__NEXT__", nxt).replace("__ERR__", "Invalid token")
        return HTMLResponse(page, status_code=401)
    resp = RedirectResponse(url=nxt, status_code=303)
    _set_session_cookie(resp, request)
    try:
        users_db.log_event("cookie.issue", user_id="owner",
                           scopes=["*"], ip=_client_ip(request),
                           user_agent=request.headers.get("user-agent"),
                           payload={"version": "v1"})
    except Exception:
        pass
    return resp


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Clear the session cookie + redirect to setup form."""
    resp = RedirectResponse(url="/", status_code=302)
    host = (request.url.hostname or "").lower()
    domain = COOKIE_DOMAIN if host.endswith("your-domain.example.com") else None
    resp.delete_cookie(COOKIE_NAME, domain=domain, path="/")
    return resp


@app.get("/manifest.webmanifest")
async def web_manifest():
    return JSONResponse(WEB_MANIFEST, media_type="application/manifest+json")


@app.get("/.well-known/assetlinks.json")
async def asset_links():
    """Digital Asset Links binding the Sentinel Network TWA package + signing
    cert to this origin (verifies the TWA → no URL bar). Returns an empty but
    valid list until TWA_PACKAGE_NAME + TWA_SHA256_CERT_FINGERPRINTS are set on
    the container, at which point the installed app runs chromeless."""
    pkg = (os.environ.get("TWA_PACKAGE_NAME") or "").strip()
    raw = (os.environ.get("TWA_SHA256_CERT_FINGERPRINTS") or "").strip()
    fingerprints = [f.strip() for f in raw.split(",") if f.strip()]
    statements = []
    if pkg and fingerprints:
        statements.append({
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": pkg,
                "sha256_cert_fingerprints": fingerprints,
            },
        })
    return JSONResponse(statements, headers={"Cache-Control": "public, max-age=300"})


# Service worker — handles incoming web-push and notification taps. Served from
# the launcher root so its scope covers the whole installed PWA/TWA. The push
# payload is the JSON the bridge sends ({title, body, level, id}).
SERVICE_WORKER_JS = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

const ICONS = { info:'/icon-192.png', success:'/icon-192.png', warning:'/icon-192.png', error:'/icon-192.png' };

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) { d = { body: (event.data && event.data.text()) || '' }; }
  const title = d.title || 'Sentinel';
  const opts = {
    body: d.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: d.id ? ('sentinel-' + d.id) : undefined,
    data: { url: '/' },
    renotify: !!d.id
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if ('focus' in c) { try { await c.navigate(url); } catch (_) {} return c.focus(); }
    }
    if (clients.openWindow) return clients.openWindow(url);
  })());
});
"""


@app.get("/sw.js")
async def service_worker():
    return Response(content=SERVICE_WORKER_JS, media_type="text/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


# Placeholder PNG icons — solid colour with a small shield glyph. Bubblewrap
# resizes these into the final APK's mipmap directories. Replace with proper
# brand artwork later by dropping `icon-192.png` and `icon-512.png` into the
# container's /app/ directory and rebuilding.
def _placeholder_icon(size: int) -> bytes:
    """Generate a flat-colour PNG with a centered shield emoji rendered via
    PIL if available; else a 1×1 transparent fallback (bubblewrap still works
    but the splash is plain)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (size, size), (28, 28, 30))
        draw = ImageDraw.Draw(img)
        # Inner gradient-ish square
        pad = int(size * 0.18)
        draw.rounded_rectangle([pad, pad, size - pad, size - pad],
                                radius=int(size * 0.18),
                                fill=(41, 151, 255))
        # White "S" centred — no font fallback is reliable without TTF, so
        # use a simple geometric mark instead.
        cx, cy = size // 2, size // 2
        r = size // 6
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255), width=max(3, size // 60))
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        # 1x1 transparent PNG.
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c63000100000005000100020d0a2db40000000049"
            "454e44ae426082"
        )


# ── Apps store (self-hosted sideload distribution) ───────────────────────────
#
# Replaces the previous "build APK → Telegram-bot sendDocument" delivery flow.
# APKs + a manifest.json live on the host at metamcp-local/sentinel-apps/,
# bind-mounted read-only into this container at /apps. The build_and_send.sh
# in each project should copy a new APK + bump its entry in manifest.json
# whenever a build ships.

APPS_DIR = Path(os.environ.get("APPS_DIR", "/apps"))


def _load_apps_manifest() -> dict:
    """Read sentinel-apps/manifest.json. Empty/missing → return empty list."""
    p = APPS_DIR / "manifest.json"
    if not p.is_file():
        return {"apps": [], "updated_at": None, "error": "manifest not found"}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"apps": [], "updated_at": None, "error": f"manifest unreadable: {e}"}


APPS_HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Sentinel Apps</title>
<meta name=theme-color content="#1c1c1e">
<style>
:root { --bg:#1c1c1e; --fg:#e8e8ea; --muted:#8e8e93; --section:#2c2c2e;
        --sep:#38383a; --accent:#2997ff; --good:#34c759; --warn:#ff9f0a; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { background:var(--bg); color:var(--fg); margin:0; min-height:100vh; }
body { font:15px/1.45 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       padding: env(safe-area-inset-top) 14px env(safe-area-inset-bottom); }
header { display:flex; align-items:center; gap:10px; padding:14px 4px; }
header a.back { color:var(--accent); text-decoration:none; font-size:14px; }
header h1 { margin:0 auto; font-size:18px; font-weight:700; }
header .spacer { width:60px; }
.empty { color:var(--muted); text-align:center; padding:60px 12px; }
.app { background:var(--section); border:1px solid var(--sep); border-radius:14px;
       padding:14px; margin:0 auto 12px; max-width:560px; }
.app .head { display:flex; gap:12px; align-items:flex-start; }
.app .ico { font-size:34px; line-height:1; flex-shrink:0; }
.app .meta { flex:1; min-width:0; }
.app .name { font-size:16px; font-weight:600; line-height:1.2; }
.app .sub  { color:var(--muted); font-size:11px; margin-top:2px; }
.app .desc { font-size:13px; color:#d2d2d4; margin:10px 0 0; }
.app .row  { display:flex; gap:8px; align-items:center; margin-top:14px;
             flex-wrap:wrap; }
.btn { background:var(--accent); color:var(--button-text); padding:9px 16px; border-radius:10px;
       font-size:14px; font-weight:600; text-decoration:none; border:0; cursor:pointer; }
.btn:active { background:#0a84ff; }
.btn.ghost { background:transparent; color:var(--accent); border:1px solid var(--accent); }
.tag { font-size:10px; padding:3px 7px; border-radius:5px; background:#444;
       color:#d2d2d4; }
.tag.size { background:#2c4f6b; color:#cfe5fa; }
.tag.sdk  { background:#3a3a3a; }
.changelog { font-size:11px; color:var(--muted); margin-top:10px;
             line-height:1.5; background:#222; padding:10px 12px; border-radius:8px;
             border-left:2px solid var(--accent); }
.vtoggle { margin-top:12px; font-size:12px; color:var(--accent); cursor:pointer;
           user-select:none; display:inline-block; }
.vtoggle:active { opacity:.6; }
.vlist { margin-top:10px; border-top:1px solid var(--sep); }
.vrow { display:flex; gap:8px; align-items:center; flex-wrap:wrap;
        padding:10px 0; border-bottom:1px solid var(--sep); }
.vrow .vver { font-weight:600; font-size:13px; min-width:54px; }
.vrow .vdate { color:var(--muted); font-size:11px; }
.vrow .vdl { margin-left:auto; }
.btn.sm { padding:6px 12px; font-size:12px; }
.vchg { font-size:11px; color:var(--muted); line-height:1.5; width:100%;
        margin-top:2px; }
.err { background:#3a2020; border:1px solid #6a2929; color:#ffd2d2; padding:10px 12px;
       border-radius:10px; margin:0 auto 12px; max-width:560px; font-size:12px; }
footer { color:var(--muted); font-size:10px; text-align:center; padding:18px 0 24px; }
.intro { text-align:center; color:var(--muted); font-size:12px; max-width:560px;
         margin:0 auto 12px; }
.tabs { display:flex; gap:8px; justify-content:center; max-width:560px;
        margin:4px auto 16px; position:sticky; top:0; z-index:5;
        background:var(--bg); padding:6px 0; }
.tab { flex:1; background:var(--section); border:1px solid var(--sep); color:var(--muted);
       font:600 14px/1 inherit; padding:11px 10px; border-radius:11px; cursor:pointer;
       display:flex; align-items:center; justify-content:center; gap:7px; }
.tab.active { color:var(--fg); border-color:var(--accent); background:#22364a; }
.tab .cnt { font-size:11px; background:var(--sep); color:var(--fg); border-radius:9px;
            padding:1px 7px; min-width:20px; }
.tab.active .cnt { background:var(--accent); color:var(--button-text); }
.btn .sz { opacity:.7; font-weight:500; margin-left:2px; }
.certnote { background:#1f2c1f; border:1px solid #2f5a2f; color:#cfe9cf; border-radius:11px;
            padding:11px 13px; margin:0 auto 12px; max-width:560px; font-size:12px; line-height:1.6; }
.certnote a { color:var(--good); font-weight:600; }
.certnote code { display:inline-block; background:#0e160e; border:1px solid #2f5a2f;
                 border-radius:6px; padding:5px 8px; margin:4px 0; font-size:11px;
                 word-break:break-all; user-select:all; -webkit-user-select:all; }
</style></head><body>

<header>
  <a class=back href="/">← Back</a>
  <h1>📦 Apps</h1>
  <div class=spacer></div>
</header>

<div class=intro>Your one-click setup — install any Sentinel app on Android or Windows.</div>

<div class=tabs>
  <button class="tab active" id=tab-android data-tab=android onclick="switchTab('android')">📱 Android</button>
  <button class=tab id=tab-windows data-tab=windows onclick="switchTab('windows')">🪟 Windows</button>
</div>

<div id=content><div class=empty>Loading…</div></div>

<footer id=foot>Android: tap Install to download the APK, then sideload (allow "Install unknown apps" once).</footer>

<script>
function bytes(n) {
  if (!n) return '?';
  const u = ['B','KB','MB','GB']; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v < 10 ? 1 : 0) + ' ' + u[i];
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function toggleVers(id) {
  const el = document.getElementById('vlist_' + id);
  const tg = document.getElementById('vtoggle_' + id);
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  tg.textContent = open ? tg.dataset.show : '▾ Hide older versions';
}

let APPS = [];
let TAB = 'android';

function hasPlatform(a, plat) {
  return (a.versions || []).some(v => (v.artifacts || []).some(x => x.platform === plat));
}
function platVers(a, plat) {
  return (a.versions || []).filter(v => (v.artifacts || []).some(x => x.platform === plat));
}
function artHref(id, file) {
  return `/apps/${encodeURIComponent(id)}/${file.split('/').map(encodeURIComponent).join('/')}`;
}
function winVerOf(arts) {
  for (const x of arts) { const m = (x.file || '').match(/(\\d+\\.\\d+\\.\\d+)/); if (m) return m[1]; }
  return '';
}
function mark(id, ver) { try { localStorage.setItem('sentinel_apps_installed_' + id, ver); } catch {} }

function appCard(a, body) {
  return `<div class=app>
    <div class=head>
      <div class=ico>${esc(a.icon || '📦')}</div>
      <div class=meta>
        <div class=name>${esc(a.name)}</div>
        <div class=sub>${esc(a.package || a.id)} · ${esc(a.category || '')}</div>
      </div>
    </div>
    <div class=desc>${esc(a.description || '')}</div>
    ${body}
    ${a.homepage ? `<div class=row><a class="btn ghost" href="${esc(a.homepage)}" target=_blank>Open web app ↗</a></div>` : ''}
  </div>`;
}
function olderBlock(id, older, rowsHtml) {
  if (!older.length) return '';
  const show = `▸ Show ${older.length} older version${older.length === 1 ? '' : 's'}`;
  return `<div class=vtoggle id="vtoggle_${esc(id)}" data-show="${show}"
            onclick="toggleVers('${esc(id)}')">${show}</div>
          <div class=vlist id="vlist_${esc(id)}" style="display:none">${rowsHtml}</div>`;
}

function renderAndroid(a) {
  const vers = platVers(a, 'android');
  if (!vers.length) return '';
  const v = vers[0], older = vers.slice(1);
  const apk = (v.artifacts || []).find(x => x.kind === 'apk') || { file: v.file, size_bytes: v.size_bytes };
  let installed = '';
  try { installed = localStorage.getItem('sentinel_apps_installed_' + a.id) || ''; } catch {}
  const label = (installed && installed !== v.version) ? `↑ Update to ${esc(v.version)}` :
                installed === v.version ? `Installed · ${esc(v.version)}` :
                `↓ Install ${esc(v.version)}`;
  const rows = older.map(ov => {
    const oapk = (ov.artifacts || []).find(x => x.kind === 'apk') || { file: ov.file, size_bytes: ov.size_bytes };
    return `<div class=vrow>
        <span class=vver>v${esc(ov.version)}</span>
        <span class=vdate>${esc(ov.released || '')}</span>
        <span class="tag size">${esc(bytes(oapk.size_bytes))}</span>
        <a class="btn ghost sm vdl" href="${artHref(a.id, oapk.file)}"
           onclick="mark('${esc(a.id)}','${esc(ov.version)}')" download>↓ APK</a>
        ${ov.changelog ? `<div class=vchg>${esc(ov.changelog)}</div>` : ''}
      </div>`;
  }).join('');
  return appCard(a, `
    <div class=row>
      <a class="btn ${installed === v.version ? 'ghost' : ''}" href="${artHref(a.id, apk.file)}"
         onclick="mark('${esc(a.id)}','${esc(v.version)}')" download>${label}</a>
      <span class="tag size">${esc(bytes(apk.size_bytes))}</span>
      ${v.min_sdk ? `<span class="tag sdk">min SDK ${v.min_sdk}</span>` : ''}
    </div>
    ${v.changelog ? `<div class=changelog><strong>v${esc(v.version)}</strong> · ${esc(v.released || '')}<br>${esc(v.changelog)}</div>` : ''}
    ${olderBlock(a.id, older, rows)}`);
}

function winBtns(id, arts, small) {
  return arts.filter(x => x.platform === 'windows').map(x => {
    const lbl = x.kind === 'msi' ? '🪟 MSI' : x.kind === 'nsis' ? '🪟 Setup .exe' : esc(x.label || x.kind);
    return `<a class="btn ${small ? 'ghost sm' : ''}" href="${artHref(id, x.file)}" download
              title="${esc(x.label || '')}">${lbl}<span class=sz>${esc(bytes(x.size_bytes))}</span></a>`;
  }).join('');
}
function renderWindows(a) {
  const vers = platVers(a, 'windows');
  if (!vers.length) return '';
  const v = vers[0], older = vers.slice(1);
  const wins = (v.artifacts || []).filter(x => x.platform === 'windows');
  const wv = winVerOf(wins) || v.version;
  const rows = older.map(ov => {
    const ow = (ov.artifacts || []).filter(x => x.platform === 'windows');
    return `<div class=vrow>
        <span class=vver>${esc(winVerOf(ow) || ov.version)}</span>
        <span class=vdate>${esc(ov.released || '')}</span>
        ${winBtns(a.id, ow, true)}
        ${ov.changelog ? `<div class=vchg>${esc(ov.changelog)}</div>` : ''}
      </div>`;
  }).join('');
  return appCard(a, `
    <div class=row>${winBtns(a.id, wins, false)}</div>
    ${v.changelog ? `<div class=changelog><strong>${esc(wv)}</strong> · ${esc(v.released || '')}<br>${esc(v.changelog)}</div>` : ''}
    ${olderBlock(a.id, older, rows)}`);
}

function renderTab() {
  const list = APPS.filter(a => hasPlatform(a, TAB));
  const foot = document.getElementById('foot');
  if (foot) foot.textContent = TAB === 'android'
    ? 'Android: tap Install to download the APK, then sideload (allow "Install unknown apps" once).'
    : 'Windows: download the MSI (managed install) or the Setup .exe (one-click). Installers are code-signed by the Sentinel certificate.';
  const c = document.getElementById('content');
  if (!list.length) { c.innerHTML = `<div class=empty>No ${esc(TAB)} apps yet.</div>`; return; }
  const cards = list.map(a => TAB === 'android' ? renderAndroid(a) : renderWindows(a)).join('');
  const cert = TAB === 'windows'
    ? `<div class=certnote>🔐 <b>New device? Trust the signer once.</b>
        <a href="/apps/trust-cert" download>Get the certificate</a>, then run (no admin needed):
        <br><code>certutil -addstore -user -f Root "%USERPROFILE%/Downloads/SentinelCodeSigning.cer"</code><br>
        Or double-click the .cer → Install → <b>change the store to “Trusted Root Certification Authorities”</b>
        (the wizard wrongly defaults to “Personal”). After that the desktop apps install with no
        “Unknown publisher” warning. A first download may still hit SmartScreen → “More info → Run anyway”.</div>`
    : '';
  c.innerHTML = cert + cards;
}
function switchTab(t) {
  TAB = t;
  document.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.dataset.tab === t));
  renderTab();
}

async function load() {
  let data;
  try {
    const r = await fetch('/api/apps', { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    data = await r.json();
  } catch (e) {
    document.getElementById('content').innerHTML =
      `<div class=err>Failed to load app catalogue: ${esc(e.message)}</div>`;
    return;
  }
  if (data.error) {
    document.getElementById('content').innerHTML = `<div class=err>${esc(data.error)}</div>`;
    return;
  }
  APPS = data.apps || [];
  const aN = APPS.filter(a => hasPlatform(a, 'android')).length;
  const wN = APPS.filter(a => hasPlatform(a, 'windows')).length;
  document.getElementById('tab-android').innerHTML = `📱 Android <span class=cnt>${aN}</span>`;
  document.getElementById('tab-windows').innerHTML = `🪟 Windows <span class=cnt>${wN}</span>`;
  if (!APPS.filter(a => hasPlatform(a, TAB)).length && wN) TAB = 'windows';
  renderTab();
}
load();
</script>
</body></html>"""


def _require_apps_install(request: Request) -> "RedirectResponse | JSONResponse | None":
    """Auth-perms v2 gate for /apps/*. Returns a response to short-circuit
    on failure, or None if the request may proceed. Beta users without the
    apps.install scope get 403; unauthenticated requests get 302 to setup."""
    payload = _get_payload(request)
    if payload is None:
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        return RedirectResponse(url="/?next=/apps", status_code=302)
    if not auth_v2.has_scope(payload, "apps.install"):
        try:
            users_db.log_event("access.denied", user_id=payload.get("user_id"),
                               jti=payload.get("jti") or None,
                               ip=_client_ip(request),
                               payload={"route": "/apps", "scope": "apps.install"})
        except Exception:
            pass
        return JSONResponse({"error": "missing scope: apps.install"}, status_code=403)
    return None


@app.get("/apps", response_class=HTMLResponse)
async def apps_index(request: Request):
    """Self-hosted sideload distribution UI. Owner + beta users with apps.install."""
    blocked = _require_apps_install(request)
    if blocked is not None:
        return blocked
    return HTMLResponse(APPS_HTML)


@app.get("/api/apps")
async def apps_manifest(request: Request):
    """JSON catalogue — drives the apps_index page."""
    blocked = _require_apps_install(request)
    if blocked is not None:
        return blocked
    return JSONResponse(_load_apps_manifest())


@app.get("/apps/trust-cert")
async def apps_trust_cert(request: Request):
    """Serve the public code-signing certificate (.cer). Installing it into
    'Trusted Root Certification Authorities' on a Windows device makes the
    signed desktop installers run without an 'Unknown publisher' warning."""
    blocked = _require_apps_install(request)
    if blocked is not None:
        return blocked
    cer = APPS_DIR / "SentinelCodeSigning.cer"
    if not cer.is_file():
        return JSONResponse({"error": "certificate not published"}, status_code=404)
    return FileResponse(str(cer), media_type="application/x-x509-ca-cert",
                        filename="SentinelCodeSigning.cer")


@app.get("/apps/{app_id}/{path:path}")
async def apps_download(app_id: str, path: str, request: Request):
    """Serve an APK from /apps/<app_id>/<path>. path is the `file` field
    from manifest (e.g. `v0.2.2/app.apk`). Path-traversal-guarded — any
    '..' or absolute segment is rejected."""
    blocked = _require_apps_install(request)
    if blocked is not None:
        return blocked
    if not app_id.replace("-", "").replace("_", "").isalnum():
        return JSONResponse({"error": "bad app id"}, status_code=400)
    # Reject path traversal — split + check no segment is '..' or empty.
    parts = [p for p in path.split("/") if p]
    if any(p == ".." or not p for p in parts):
        return JSONResponse({"error": "bad path"}, status_code=400)
    target = (APPS_DIR / app_id / Path(*parts)).resolve()
    base   = (APPS_DIR / app_id).resolve()
    # Final defence: resolved path must remain under APPS_DIR/<app_id>.
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"error": "bad path"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"error": "file not found", "path": str(target)}, status_code=404)
    # Filename Android shows in its "Open with…" download notification —
    # we tack on app+version so the user can tell rebuilds apart in their
    # Downloads folder.
    suggested = f"{app_id}-{target.parent.name}-{target.name}"
    media = {
        ".apk": "application/vnd.android.package-archive",
        ".msi": "application/x-msi",
        ".exe": "application/vnd.microsoft.portable-executable",
    }.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(path=str(target), media_type=media, filename=suggested)


@app.get("/icon-192.png")
async def icon_192():
    from fastapi.responses import Response
    return Response(content=_placeholder_icon(192), media_type="image/png")


@app.get("/icon-512.png")
async def icon_512():
    from fastapi.responses import Response
    return Response(content=_placeholder_icon(512), media_type="image/png")


# ── License Registry console (proxies watchdog v2 :8200) ─────────────────────
# Central place to view + revoke issued license keys. The registry is the
# authority (watchdog v2); this is a thin owner/licenses.manage-gated surface
# over it. Metadata only — no bearer secrets ever reach this app. Revocation
# is terminal and is recorded both here (users_db audit) and at the registry.
async def _registry_call(method: str, path: str, timeout: float = 8.0) -> "tuple[int, Any]":
    """Call the watchdog License Registry / host API with the service token.
    Returns (status_code, parsed_json). Never raises — network/timeout failures
    map to 5xx with an error dict the UI can render."""
    if not LICENSE_REGISTRY_TOKEN:
        return 503, {"error": "registry_unconfigured"}
    url = f"{LICENSE_REGISTRY_URL}{path}"
    headers = {"X-Sentinel-Service-Token": LICENSE_REGISTRY_TOKEN,
               "accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.request(method, url, headers=headers)
    except httpx.TimeoutException:
        return 504, {"error": "registry_timeout"}
    except Exception as e:
        return 502, {"error": "registry_unreachable", "detail": str(e)[:200]}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"error": "bad_registry_response"}


def _require_licenses(request: Request) -> "RedirectResponse | JSONResponse | None":
    """Auth gate for the license console. Owner (v1 '*') passes implicitly;
    scoped v2 users need licenses.manage. Mirrors _require_apps_install."""
    payload = _get_payload(request)
    wants_json = request.headers.get("accept", "").startswith("application/json")
    if payload is None:
        if wants_json:
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        return RedirectResponse(url="/?next=/admin/licenses", status_code=302)
    if not auth_v2.has_scope(payload, "licenses.manage"):
        try:
            users_db.log_event("access.denied", user_id=payload.get("user_id"),
                               jti=payload.get("jti") or None,
                               ip=_client_ip(request),
                               payload={"route": "/admin/licenses", "scope": "licenses.manage"})
        except Exception:
            pass
        if wants_json:
            return JSONResponse({"error": "missing scope: licenses.manage"}, status_code=403)
        return RedirectResponse(url="/", status_code=302)
    return None


@app.get("/admin/licenses", response_class=HTMLResponse)
async def admin_licenses(request: Request):
    blocked = _require_licenses(request)
    if blocked is not None:
        return blocked
    return HTMLResponse(ADMIN_HTML.replace("__BODY__", LICENSES_BODY))


@app.get("/api/licenses")
async def api_licenses(request: Request, status: str = "", tier: str = ""):
    blocked = _require_licenses(request)
    if blocked is not None:
        return blocked
    qs = []
    if status in ("active", "revoked"):
        qs.append(f"status={status}")
    if tier in ("community", "family"):
        qs.append(f"tier={tier}")
    qs.append("limit=2000")
    code, body = await _registry_call("GET", "/api/v2/licenses?" + "&".join(qs))
    return JSONResponse(body, status_code=code)


@app.post("/api/licenses/{key_id}/revoke")
async def api_license_revoke(key_id: str, request: Request):
    blocked = _require_licenses(request)
    if blocked is not None:
        return blocked
    key_id = (key_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key_id):
        return JSONResponse({"error": "bad_key_id"}, status_code=400)
    code, body = await _registry_call("POST", f"/api/v2/licenses/{key_id}/revoke")
    if code == 200:
        payload = _get_payload(request) or {}
        try:
            users_db.log_event("license.revoke", user_id=payload.get("user_id"),
                               jti=payload.get("jti") or None,
                               ip=_client_ip(request),
                               payload={"key_id": key_id})
        except Exception:
            pass
    return JSONResponse(body, status_code=code)


# ── Network discovery (LAN scan via the host watchdog API) ───────────────────
@app.get("/api/discover")
async def api_discover(request: Request, subnet: str = "192.168.50.0/24"):
    """LAN device discovery (IP + MAC + hostname). The dashboard container has
    no L2 access, so this proxies to the watchdog API on the host, which owns
    the real LAN + ARP table. Owner-only."""
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    if not re.fullmatch(r"[0-9.]{7,18}/[0-9]{1,2}", subnet or ""):
        return JSONResponse({"error": "bad_subnet"}, status_code=400)
    # The host ping-sweep + reverse-DNS can take a few seconds on first call;
    # the host caches ~25s, so give it headroom past the default 8s.
    code, body = await _registry_call("GET", f"/api/v2/net/discover?subnet={subnet}", timeout=20.0)
    return JSONResponse(body, status_code=code)


@app.get("/api/ports")
async def api_ports(request: Request):
    """Host listening ports (read-only) via the watchdog API on the host. The
    dashboard container can't see the host's listeners, so it proxies. Owner-only."""
    if not _is_owner(request):
        return JSONResponse({"error": "owner_only"}, status_code=403)
    code, body = await _registry_call("GET", "/api/v2/net/ports", timeout=12.0)
    return JSONResponse(body, status_code=code)


# ── Notifications proxy → agent dashboard bridge (:8098) ─────────────────────
# The browser only ever talks to this launcher (same-origin); we relay to the
# host bridge over host.docker.internal, forwarding the owner cookie so the
# bridge's _verify_apk_cookie authenticates the call. One shared feed, two hubs.
async def _notify_bridge_call(method: str, path: str, request: Request,
                              json_body: "dict | None" = None) -> "tuple[int, Any]":
    """Relay to the bridge's notification/push API, forwarding the owner's
    session cookie (and an optional JSON body). Never raises — network failures
    map to a 5xx error dict."""
    raw = request.cookies.get(COOKIE_NAME, "")
    url = f"{NOTIFY_BRIDGE_URL}{path}"
    headers = {"accept": "application/json"}
    if raw:
        headers["Cookie"] = f"{COOKIE_NAME}={raw}"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as c:
            r = await c.request(method, url, headers=headers, json=json_body)
    except httpx.TimeoutException:
        return 504, {"error": "bridge_timeout"}
    except Exception as e:
        return 502, {"error": "bridge_unreachable", "detail": str(e)[:200]}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"error": "bad_bridge_response"}


@app.get("/api/notifications")
async def api_notifications(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    code, body = await _notify_bridge_call("GET", "/api/notifications", request)
    return JSONResponse(body, status_code=code)


@app.post("/api/notifications/read-all")
async def api_notifications_read_all(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    code, body = await _notify_bridge_call("POST", "/api/notifications/read-all", request)
    return JSONResponse(body, status_code=code)


@app.post("/api/notifications/{nid}/read")
async def api_notifications_read(nid: int, request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    code, body = await _notify_bridge_call("POST", f"/api/notifications/{nid}/read", request)
    return JSONResponse(body, status_code=code)


# ── Web Push proxy (subscription lives in the bridge; SW lives here) ─────────
@app.get("/api/push/vapid-public")
async def api_push_vapid_public(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    code, body = await _notify_bridge_call("GET", "/api/push/vapid-public", request)
    return JSONResponse(body, status_code=code)


@app.post("/api/push/subscribe")
async def api_push_subscribe(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_json"}, status_code=400)
    code, body = await _notify_bridge_call("POST", "/api/push/subscribe", request, json_body=payload)
    return JSONResponse(body, status_code=code)


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_json"}, status_code=400)
    code, body = await _notify_bridge_call("POST", "/api/push/unsubscribe", request, json_body=payload)
    return JSONResponse(body, status_code=code)


# ── Notification preferences proxy (store lives in the bridge) ───────────────
@app.get("/api/notify/prefs")
async def api_notify_prefs_get(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    code, body = await _notify_bridge_call("GET", "/api/notify/prefs", request)
    return JSONResponse(body, status_code=code)


@app.post("/api/notify/prefs")
async def api_notify_prefs_set(request: Request):
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_json"}, status_code=400)
    code, body = await _notify_bridge_call("POST", "/api/notify/prefs", request, json_body=payload)
    return JSONResponse(body, status_code=code)


# ── auth-perms v2 — user admin + redemption flow ─────────────────────────────
# Spec: metamcp-local/docs/auth-perms-v2.md §7 + §8
#
# Owner-only routes under /admin/*. Beta users redeem invites at /auth/redeem
# and can introspect their own session at /auth/whoami.

ADMIN_HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Sentinel · Users</title>
<meta name=theme-color content="#1c1c1e">
<style>
:root { --bg:#1c1c1e; --fg:#e8e8ea; --muted:#8e8e93; --section:#2c2c2e;
        --sep:#38383a; --accent:#2997ff; --good:#34c759; --warn:#ff9f0a; --bad:#ff453a; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html,body { background:var(--bg); color:var(--fg); margin:0; min-height:100vh; }
body { font:14.5px/1.45 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       padding: env(safe-area-inset-top) 14px env(safe-area-inset-bottom);
       max-width:760px; margin-left:auto; margin-right:auto; }
header { display:flex; align-items:center; gap:10px; padding:14px 4px; }
header a.back { color:var(--accent); text-decoration:none; font-size:14px; }
header h1 { margin:0 auto; font-size:18px; font-weight:700; }
header .spacer { width:60px; }
section { background:var(--section); border:1px solid var(--sep); border-radius:14px;
          padding:14px; margin:10px 0; }
section h2 { margin:0 0 10px; font-size:13px; text-transform:uppercase;
             letter-spacing:0.06em; color:var(--muted); font-weight:600; }
.user-row { display:flex; align-items:center; gap:10px; padding:10px 4px;
            border-bottom:1px solid var(--sep); text-decoration:none; color:var(--fg); }
.user-row:last-child { border-bottom:none; }
.user-row .name { font-weight:600; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; }
.user-row .meta { color:var(--muted); font-size:11px; }
.badge { font-size:10px; padding:2px 7px; border-radius:6px; font-weight:600; }
.badge.active { background:rgba(52,199,89,0.18); color:var(--good); }
.badge.revoked { background:rgba(255,69,58,0.18); color:var(--bad); }
.badge.never { background:rgba(142,142,147,0.18); color:var(--muted); }
.empty { color:var(--muted); text-align:center; padding:20px 0; font-size:13px; }
form label { display:block; color:var(--muted); font-size:11px;
             text-transform:uppercase; letter-spacing:0.06em; margin:10px 0 4px; }
input[type=text], input[type=number] { width:100%; padding:10px; border-radius:8px;
                                       border:1px solid var(--sep); background:#222;
                                       color:var(--fg); font:14px monospace; outline:none; }
input[type=text]:focus, input[type=number]:focus { border-color:var(--accent); }
.scopes-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px 12px; margin-top:6px;
               font-size:13px; max-height:280px; overflow-y:auto; padding:6px;
               background:#222; border-radius:8px; border:1px solid var(--sep); }
.scopes-grid label { color:var(--fg); font-size:12.5px; text-transform:none;
                     letter-spacing:0; margin:0; display:flex; align-items:center; gap:6px;
                     cursor:pointer; }
.scopes-grid input[type=checkbox] { margin:0; }
button, .btn { background:var(--accent); color:var(--button-text); border:0; padding:10px 16px;
               border-radius:8px; font-size:14px; font-weight:600; cursor:pointer;
               text-decoration:none; display:inline-block; }
button:active, .btn:active { background:#0a84ff; }
.btn.ghost { background:transparent; color:var(--accent); border:1px solid var(--accent); }
.btn.bad { background:transparent; color:var(--bad); border:1px solid var(--bad); }
.row { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
.notice { background:rgba(41,151,255,0.10); border:1px solid var(--accent);
          padding:10px 14px; border-radius:10px; margin:10px 0; font-size:13px;
          word-break:break-all; }
.notice strong { color:var(--accent); }
.tag { font-size:10.5px; padding:2px 7px; border-radius:5px; background:#444;
       color:#d2d2d4; margin:2px 4px 2px 0; display:inline-block; }
footer { color:var(--muted); font-size:10px; text-align:center; padding:18px 0 24px; }
.event { font-size:12px; padding:6px 4px; border-bottom:1px solid var(--sep);
         display:flex; gap:8px; align-items:baseline; }
.event:last-child { border-bottom:none; }
.event .ts { color:var(--muted); font-family:monospace; font-size:11px; flex-shrink:0; }
.event .ev { font-weight:600; min-width:110px; }
</style></head><body>
__BODY__
<footer>auth-perms v2 · azfar — owner only</footer>
</body></html>"""


# Body spliced into ADMIN_HTML for the /admin/licenses console. Pure client-side
# render: the list + filter pills hit /api/licenses (which proxies the registry).
LICENSES_BODY = """
<header><a class=back href="/">← Suite</a><h1>📜 Licenses</h1><div class=spacer></div></header>
<section>
  <div class=row style="margin-top:0">
    <button class="btn ghost lic-f" data-f="all">All</button>
    <button class="btn ghost lic-f" data-f="active">Active</button>
    <button class="btn ghost lic-f" data-f="revoked">Revoked</button>
    <button class="btn ghost" id=lic-refresh style="margin-left:auto" title="Refresh">↻</button>
  </div>
</section>
<section id=lic-list><div class=empty>Loading…</div></section>
<script>
let licFilter = 'all';
function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function licBadge(s){
  const cls = s === 'revoked' ? 'revoked' : (s === 'expired' ? 'never' : 'active');
  return '<span class="badge '+cls+'">'+esc(s)+'</span>';
}
function fmtDate(v){ if(!v) return '—'; try { return String(v).slice(0,16).replace('T',' '); } catch(e){ return '—'; } }
function paintPills(){
  document.querySelectorAll('.lic-f').forEach(function(x){
    const on = x.dataset.f === licFilter;
    x.style.background = on ? 'var(--accent)' : '';
    x.style.color = on ? 'white' : '';
  });
}
async function loadLicenses(){
  const list = document.getElementById('lic-list');
  list.innerHTML = '<div class=empty>Loading…</div>';
  let q = '';
  if (licFilter === 'active' || licFilter === 'revoked') q = '?status='+licFilter;
  let r, data;
  try {
    r = await fetch('/api/licenses'+q, {credentials:'same-origin', headers:{'accept':'application/json'}});
    data = await r.json();
  } catch(e){ list.innerHTML = '<div class=empty>Could not reach the server.</div>'; return; }
  if (!r.ok){
    let msg = (data && data.error) || ('HTTP '+r.status);
    if (msg === 'registry_unconfigured') msg = 'License registry not configured (no service token).';
    if (msg === 'registry_unreachable' || msg === 'registry_timeout') msg = 'License registry unreachable.';
    list.innerHTML = '<div class=empty>'+esc(msg)+'</div>'; return;
  }
  const rows = (data.licenses || []);
  if (!rows.length){ list.innerHTML = '<div class=empty>No licenses issued yet.</div>'; return; }
  list.innerHTML = rows.map(function(L){
    const st = L.display_status || L.status || 'active';
    const who = L.issued_to ? esc(L.issued_to) : 'unassigned';
    const inst = L.instance ? esc(L.instance) : '—';
    const revokeBtn = (st === 'revoked') ? ''
      : '<button class="btn bad lic-revoke" data-id="'+esc(L.key_id)+'" style="padding:6px 12px">Revoke</button>';
    return '<div class=user-row style="flex-wrap:wrap;align-items:flex-start">'
      + '<div style="flex:1;min-width:0">'
      +   '<div class=name style="font-family:monospace">'+esc(L.key_id)+'</div>'
      +   '<div class=meta>'+esc(L.tier||'—')+' · '+who+' · '+inst+'</div>'
      +   '<div class=meta>issued '+fmtDate(L.created_at||L.issued_at)+' · expires '+fmtDate(L.expires_at)+'</div>'
      + '</div>'
      + '<div style="display:flex;align-items:center;gap:8px;margin-left:auto">'+licBadge(st)+revokeBtn+'</div>'
      + '</div>';
  }).join('');
  list.querySelectorAll('.lic-revoke').forEach(function(b){
    b.onclick = function(){ revokeLicense(b.dataset.id); };
  });
}
async function revokeLicense(keyId){
  if (!confirm('Revoke '+keyId+'?\\n\\nThis is permanent — the key cannot be reactivated.')) return;
  let r, data;
  try {
    r = await fetch('/api/licenses/'+encodeURIComponent(keyId)+'/revoke',
        {method:'POST', credentials:'same-origin', headers:{'accept':'application/json'}});
    data = await r.json();
  } catch(e){ alert('Revoke failed: could not reach the server.'); return; }
  if (!r.ok){ alert('Revoke failed: '+esc((data&&data.error)||('HTTP '+r.status))); return; }
  loadLicenses();
}
document.querySelectorAll('.lic-f').forEach(function(b){
  b.onclick = function(){ licFilter = b.dataset.f; paintPills(); loadLicenses(); };
});
document.getElementById('lic-refresh').onclick = loadLicenses;
paintPills();
loadLicenses();
</script>
"""


def _scopes_picker_html(catalogue: dict, checked: list[str], name: str = "scopes") -> str:
    """Render a multi-checkbox scope picker grouped visually by pillar.
    Owner-only — beta users never see this UI."""
    if not catalogue:
        return '<div class=empty>No scopes in catalogue (data/scopes.yaml unreadable).</div>'
    rows = []
    for scope_id in sorted(catalogue.keys()):
        entry = catalogue[scope_id]
        label = (entry or {}).get("label", scope_id)
        c = "checked" if scope_id in checked else ""
        rows.append(
            f'<label><input type=checkbox name="{name}" value="{scope_id}" {c}>'
            f'<span><code>{scope_id}</code> <span style="color:var(--muted);font-size:11px">— {label}</span></span></label>'
        )
    return '<div class=scopes-grid>' + "".join(rows) + '</div>'


def _esc(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _admin_guard(request: Request):
    """Owner-only gate for /admin/*. Returns RedirectResponse on fail, None to proceed."""
    if not _is_owner(request):
        return RedirectResponse(url="/?next=/admin/users", status_code=302)
    return None


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_list(request: Request):
    blocked = _admin_guard(request)
    if blocked is not None:
        return blocked
    users = users_db.list_users()
    scopes_catalogue = _load_scopes_yaml()
    rows = []
    for u in users:
        n = len(u.get("scopes") or [])
        if u.get("revoked_at"):
            badge = '<span class="badge revoked">revoked</span>'
        elif u.get("last_active_at"):
            badge = '<span class="badge active">active</span>'
        else:
            badge = '<span class="badge never">never used</span>'
        last = (u.get("last_active_at") or u.get("created_at") or "")[:16].replace("T", " ")
        rows.append(
            f'<a class=user-row href="/admin/users/{_esc(u["id"])}">'
            f'<div><div class=name>{_esc(u.get("handle") or u["id"])}</div>'
            f'<div class=meta>{_esc(u["id"])} · {n} scope{"" if n == 1 else "s"} · last {_esc(last)}</div></div>'
            f'<div style="margin-left:auto">{badge}</div></a>'
        )
    user_list_html = "".join(rows) if rows else '<div class=empty>No beta users yet.</div>'
    body = (
        '<header><a class=back href="/">← Suite</a><h1>🔑 Users</h1><div class=spacer></div></header>'
        '<section><h2>Beta users</h2>' + user_list_html + '</section>'
        '<section><h2>Invite a new user</h2>'
        '<form method=POST action="/admin/users">'
        '<label>User id <span style="color:var(--muted);text-transform:none">— slug, [a-z0-9_-]+, max 32</span></label>'
        '<input type=text name=id required pattern="[a-z0-9_-]{1,32}" maxlength=32 placeholder="alice">'
        '<label>Handle <span style="color:var(--muted);text-transform:none">— display name</span></label>'
        '<input type=text name=handle placeholder="Alice (TG @alice)">'
        '<label>Expires in days <span style="color:var(--muted);text-transform:none">— blank for 90d default</span></label>'
        '<input type=number name=expires_in_days min=1 max=3650 placeholder="90">'
        '<label>Scopes</label>'
        + _scopes_picker_html(scopes_catalogue, []) +
        '<div class=row><button type=submit>Create user</button></div>'
        '</form></section>'
        '<section><h2>Recent events</h2>'
        '<div style="text-align:right;margin-bottom:6px"><a class="btn ghost" style="padding:6px 12px" href="/admin/audit">View full audit log →</a></div>'
        + _events_html(users_db.recent_events(limit=10)) +
        '</section>'
    )
    return HTMLResponse(ADMIN_HTML.replace("__BODY__", body))


@app.post("/admin/users")
async def admin_users_create(request: Request):
    blocked = _admin_guard(request)
    if blocked is not None:
        return blocked
    form = await request.form()
    user_id = (form.get("id") or "").strip().lower()
    handle = (form.get("handle") or "").strip()
    scopes = [s for s in form.getlist("scopes") if s]
    exp_raw = (form.get("expires_in_days") or "").strip()
    try:
        expires_in_days = int(exp_raw) if exp_raw else None
    except ValueError:
        expires_in_days = None
    try:
        users_db.create_user(user_id, handle, scopes, expires_in_days=expires_in_days)
    except ValueError as e:
        body = f'<header><a class=back href="/admin/users">← Users</a><h1>Error</h1><div class=spacer></div></header><section><div class=empty style="color:var(--bad)">⚠ {_esc(e)}</div></section>'
        return HTMLResponse(ADMIN_HTML.replace("__BODY__", body), status_code=400)
    except sqlite3.IntegrityError:
        body = f'<header><a class=back href="/admin/users">← Users</a><h1>Error</h1><div class=spacer></div></header><section><div class=empty style="color:var(--bad)">⚠ user id <code>{_esc(user_id)}</code> already exists</div></section>'
        return HTMLResponse(ADMIN_HTML.replace("__BODY__", body), status_code=400)
    users_db.log_event("user.create", user_id=user_id, scopes=scopes,
                       ip=_client_ip(request),
                       user_agent=request.headers.get("user-agent"),
                       payload={"handle": handle, "expires_in_days": expires_in_days})
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=303)


def _events_html(events: list[dict]) -> str:
    if not events:
        return '<div class=empty>No events yet.</div>'
    parts = []
    for e in events:
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        ev = e.get("event") or "?"
        uid = e.get("user_id") or "—"
        extra = ""
        if e.get("scopes"):
            extra = f' · {len(e["scopes"])} scope{"" if len(e["scopes"]) == 1 else "s"}'
        if e.get("payload"):
            keys = list(e["payload"].keys())
            extra += f" · {', '.join(keys[:3])}"
        parts.append(
            f'<div class=event><span class=ts>{_esc(ts)}</span>'
            f'<span class=ev>{_esc(ev)}</span>'
            f'<span>{_esc(uid)}{_esc(extra)}</span></div>'
        )
    return "".join(parts)


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(user_id: str, request: Request):
    blocked = _admin_guard(request)
    if blocked is not None:
        return blocked
    user = users_db.get_user(user_id)
    if not user:
        body = '<header><a class=back href="/admin/users">← Users</a><h1>Not found</h1><div class=spacer></div></header><section><div class=empty>No such user.</div></section>'
        return HTMLResponse(ADMIN_HTML.replace("__BODY__", body), status_code=404)
    scopes_catalogue = _load_scopes_yaml()
    invites = users_db.list_invites_for_user(user_id)
    events = users_db.recent_events(limit=50, user_id=user_id)

    invites_html_parts = []
    now = users_db._iso_now()
    for inv in invites:
        status = (
            "redeemed" if inv["redeemed_at"]
            else "expired" if inv["expires_at"] < now
            else "pending"
        )
        invites_html_parts.append(
            f'<div class=event><span class=ts>{_esc(inv["created_at"][:19].replace("T"," "))}</span>'
            f'<span class=ev>{_esc(status)}</span>'
            f'<span><code style="font-size:11px">{_esc(inv["token"][:12])}…</code> · expires {_esc(inv["expires_at"][:16].replace("T"," "))}</span></div>'
        )
    invites_html = "".join(invites_html_parts) if invites_html_parts else '<div class=empty>No invites issued yet.</div>'

    revoked_banner = ""
    if user.get("revoked_at"):
        revoked_banner = f'<div class="notice" style="background:rgba(255,69,58,0.10);border-color:var(--bad)"><strong>Revoked</strong> at {_esc(user["revoked_at"][:19].replace("T", " "))}. All issued cookies are blocklisted.</div>'

    body = (
        f'<header><a class=back href="/admin/users">← Users</a><h1>{_esc(user.get("handle") or user["id"])}</h1><div class=spacer></div></header>'
        + revoked_banner +
        '<section><h2>Identity</h2>'
        f'<div><b>id:</b> <code>{_esc(user["id"])}</code></div>'
        f'<div style="margin-top:6px"><b>handle:</b> {_esc(user.get("handle") or "—")}</div>'
        f'<div style="margin-top:6px"><b>created:</b> {_esc(user["created_at"][:16].replace("T", " "))}</div>'
        f'<div style="margin-top:6px"><b>expires:</b> {_esc((user.get("expires_at") or "—")[:16].replace("T", " "))}</div>'
        f'<div style="margin-top:6px"><b>last active:</b> {_esc((user.get("last_active_at") or "never")[:16].replace("T", " "))}</div>'
        '</section>'
        '<section><h2>Scopes</h2>'
        '<form method=POST action="/admin/users/' + _esc(user_id) + '/scopes">'
        + _scopes_picker_html(scopes_catalogue, user.get("scopes") or []) +
        '<div class=row><button type=submit>Save scopes</button></div>'
        '</form></section>'
        '<section><h2>Invites</h2>'
        '<form method=POST action="/admin/users/' + _esc(user_id) + '/invite" style="margin-bottom:10px">'
        '<div class=row><button type=submit'
        + (' disabled style="opacity:0.4"' if user.get("revoked_at") else '') +
        '>+ Mint redemption link</button>'
        '<span style="color:var(--muted);font-size:12px;align-self:center">24h, single-use</span></div>'
        '</form>'
        + invites_html +
        '</section>'
        '<section><h2>Audit log (this user)</h2>'
        + _events_html(events) +
        '</section>'
    )

    if not user.get("revoked_at"):
        body += (
            '<section><h2>Danger zone</h2>'
            '<form method=POST action="/admin/users/' + _esc(user_id) + '/revoke" onsubmit="return confirm(\'Revoke this user? All cookies are immediately blocklisted.\');">'
            '<div class=row><button type=submit class="btn bad">Revoke user + all cookies</button></div>'
            '</form></section>'
        )

    return HTMLResponse(ADMIN_HTML.replace("__BODY__", body))


@app.post("/admin/users/{user_id}/scopes")
async def admin_user_update_scopes(user_id: str, request: Request):
    blocked = _admin_guard(request)
    if blocked is not None:
        return blocked
    if not users_db.get_user(user_id):
        return JSONResponse({"error": "no such user"}, status_code=404)
    form = await request.form()
    scopes = [s for s in form.getlist("scopes") if s]
    users_db.update_scopes(user_id, scopes)
    users_db.log_event("user.scopes", user_id=user_id, scopes=scopes,
                       ip=_client_ip(request),
                       user_agent=request.headers.get("user-agent"))
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=303)


@app.post("/admin/users/{user_id}/invite")
async def admin_user_invite(user_id: str, request: Request):
    blocked = _admin_guard(request)
    if blocked is not None:
        return blocked
    user = users_db.get_user(user_id)
    if not user:
        return JSONResponse({"error": "no such user"}, status_code=404)
    if user.get("revoked_at"):
        return JSONResponse({"error": "user revoked"}, status_code=400)
    inv = users_db.create_invite(user_id)
    users_db.log_event("invite.send", user_id=user_id,
                       ip=_client_ip(request),
                       user_agent=request.headers.get("user-agent"),
                       payload={"expires_at": inv["expires_at"]})
    host = (request.url.hostname or "").lower()
    scheme = "https" if host.endswith("your-domain.example.com") else request.url.scheme
    base = f"{scheme}://{request.url.netloc}"
    redeem_url = f"{base}/auth/redeem?token={inv['token']}"
    body = (
        f'<header><a class=back href="/admin/users/{_esc(user_id)}">← Back</a><h1>New invite</h1><div class=spacer></div></header>'
        f'<section><h2>Send this to {_esc(user.get("handle") or user_id)}</h2>'
        f'<div class=notice><strong>One-time link · 24h TTL · single use</strong><br>'
        f'<a href="{_esc(redeem_url)}" style="color:var(--accent);word-break:break-all">{_esc(redeem_url)}</a></div>'
        f'<div style="color:var(--muted);font-size:12px;margin-top:8px">'
        f'Expires at {_esc(inv["expires_at"][:16].replace("T", " "))} UTC. Once opened, mints a 90-day session cookie '
        f'on <code>.your-domain.example.com</code> with the user\'s scopes.</div></section>'
    )
    return HTMLResponse(ADMIN_HTML.replace("__BODY__", body))


@app.post("/admin/users/{user_id}/revoke")
async def admin_user_revoke(user_id: str, request: Request):
    blocked = _admin_guard(request)
    if blocked is not None:
        return blocked
    user = users_db.get_user(user_id)
    if not user:
        return JSONResponse({"error": "no such user"}, status_code=404)
    n = users_db.revoke_user(user_id, reason="admin revoke")
    users_db.log_event("cookie.revoke", user_id=user_id,
                       ip=_client_ip(request),
                       user_agent=request.headers.get("user-agent"),
                       payload={"jtis_blocklisted": n})
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit(request: Request, limit: int = 200):
    blocked = _admin_guard(request)
    if blocked is not None:
        return blocked
    limit = max(10, min(int(limit), 1000))
    events = users_db.recent_events(limit=limit)
    body = (
        '<header><a class=back href="/admin/users">← Users</a><h1>Audit log</h1><div class=spacer></div></header>'
        f'<section><h2>Recent {len(events)} event(s)</h2>'
        + _events_html(events) +
        '</section>'
    )
    return HTMLResponse(ADMIN_HTML.replace("__BODY__", body))


@app.get("/api/scopes")
async def api_scopes(request: Request):
    """Scope catalogue (for admin UI dropdowns + external tooling).
    Owner-only to avoid leaking pillar structure to anonymous probes."""
    if not _is_owner(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    return JSONResponse({"scopes": _load_scopes_yaml()})


# ── auth-perms v2 — public redemption + introspection ────────────────────────


@app.get("/auth/redeem")
async def auth_redeem(token: str, request: Request):
    """Beta user lands here via the URL the owner sent them. Consume the
    invite, look up the user's scopes, mint a v2 cookie, set it on the
    apex domain, redirect home. One-time use — same 401 response for
    unknown / expired / already-used (no enumeration)."""
    if not OWNER_AUTH_TOKEN:
        return JSONResponse({"error": "server misconfigured"}, status_code=500)

    # Don't let an already-authed session silently switch identities by
    # consuming the invite. Common footgun: owner taps the invite link
    # they just created, which would overwrite their owner cookie with
    # the invitee's v2 cookie AND burn the one-time invite in the
    # process. Block here without touching the DB — the invite stays
    # redeemable for the actual recipient.
    if _is_authed(request):
        payload = _get_payload(request) or {}
        who = payload.get("user_id", "?")
        users_db.log_event(
            "invite.redeem", ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            payload={"ok": False, "reason": "already_authed",
                     "as": who, "token_prefix": token[:8]},
        )
        body = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Already signed in</title>"
            "<style>body{font:16px/1.45 system-ui;margin:24px;max-width:520px;"
            "color:#e5e5e7;background:#1c1c1e}h1{font-size:20px;margin:0 0 12px}"
            "code{background:#2c2c2e;padding:2px 6px;border-radius:4px}"
            "ul{padding-left:20px}</style></head><body>"
            f"<h1>Already signed in as <code>{_esc(who)}</code></h1>"
            "<p>Opening this invite link here would replace your session "
            "with the invitee's, and would burn the one-time invite in the "
            "process. To redeem this invite:</p><ul>"
            "<li>forward the link to the intended recipient, or</li>"
            "<li>open it in a private/incognito window.</li></ul>"
            "<p>The invite has <b>not</b> been consumed.</p>"
            "</body></html>"
        )
        return HTMLResponse(body, status_code=409)

    inv = users_db.consume_invite(token, ip=_client_ip(request))
    if inv is None:
        users_db.log_event("invite.redeem", ip=_client_ip(request),
                           user_agent=request.headers.get("user-agent"),
                           payload={"ok": False, "token_prefix": token[:8]})
        return JSONResponse(
            {"error": "invite invalid, expired, or already redeemed"},
            status_code=401,
        )
    user = users_db.get_user(inv["user_id"])
    if not user or user.get("revoked_at"):
        users_db.log_event("invite.redeem", ip=_client_ip(request),
                           payload={"ok": False, "user": inv["user_id"], "reason": "revoked"})
        return JSONResponse({"error": "user not available"}, status_code=403)
    scopes = user.get("scopes") or []
    cookie = auth_v2.issue_v2_cookie(OWNER_AUTH_TOKEN, user["id"], scopes)
    # The cookie's jti is at parts[3]. Pluck it for the audit row.
    try:
        jti = cookie.split(".")[3]
    except IndexError:
        jti = ""
    users_db.log_event("invite.redeem", user_id=user["id"], jti=jti,
                       scopes=scopes, ip=_client_ip(request),
                       user_agent=request.headers.get("user-agent"),
                       payload={"ok": True})
    users_db.log_event("cookie.issue", user_id=user["id"], jti=jti,
                       scopes=scopes, ip=_client_ip(request),
                       payload={"version": "v2"})
    users_db.mark_active(user["id"])
    resp = RedirectResponse(url="/", status_code=303)
    host = (request.url.hostname or "").lower()
    domain = COOKIE_DOMAIN if host.endswith("your-domain.example.com") else None
    resp.set_cookie(
        key=COOKIE_NAME,
        value=cookie,
        max_age=COOKIE_TTL_SEC,
        domain=domain,
        path="/",
        secure=domain is not None,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.get("/auth/whoami")
async def auth_whoami(request: Request):
    payload = _get_payload(request)
    if payload is None:
        return JSONResponse({"authenticated": False}, status_code=401)
    return JSONResponse({
        "authenticated": True,
        "user_id":       payload.get("user_id"),
        "version":       payload.get("version"),
        "scopes":        payload.get("scopes"),
        "jti":           payload.get("jti") or None,
        "issued_at":     payload.get("iat"),
    })
