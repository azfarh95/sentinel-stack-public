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
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vpn-dashboard")


# ── APK cookie auth (shared across all *.your-domain.example.com subdomains) ─────────
# Same scheme is implemented in sentinel-miniapp-v2/bridge.py and
# sentinel-smdl/app/miniapp.py so one /auth/setup hit authorises all three.
OWNER_AUTH_TOKEN = os.environ.get("OWNER_AUTH_TOKEN", "")
COOKIE_NAME      = "sentinel_apk_session"
COOKIE_DOMAIN    = ".your-domain.example.com"
COOKIE_TTL_SEC   = 90 * 24 * 3600  # 90 days


def _issue_cookie() -> str:
    """HMAC-signed `<ts>.<nonce>.<sig>` payload — signature key is OWNER_AUTH_TOKEN."""
    ts    = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    body  = f"{ts}.{nonce}"
    sig   = hmac.new(OWNER_AUTH_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify_cookie(val: str) -> bool:
    if not val or not OWNER_AUTH_TOKEN:
        return False
    try:
        body, sig = val.rsplit(".", 1)
        ts_s, _   = body.split(".", 1)
        expected  = hmac.new(OWNER_AUTH_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        return (time.time() - int(ts_s)) < COOKIE_TTL_SEC
    except Exception:
        return False


def _is_authed(request: Request) -> bool:
    return _verify_cookie(request.cookies.get(COOKIE_NAME, ""))


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
        last_seen = n.get("last_seen") or n.get("lastSeen") or ""
        is_online = bool(n.get("online", False))
        if is_online:
            online_count += 1
        norm_nodes.append({
            "name":       n.get("name", "?"),
            "ipv4":       (n.get("ip_addresses") or n.get("ipAddresses") or [None])[0],
            "user":       (n.get("user") or {}).get("name") if isinstance(n.get("user"), dict) else n.get("user", ""),
            "online":     is_online,
            "last_seen":  last_seen,
            "created_at": n.get("created_at") or n.get("createdAt") or "",
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


def probe_amneziawg() -> dict:
    """`awg show awg0 dump` → first line is interface meta, subsequent lines
    are per-peer entries (tab-separated):
        pubkey  preshared  endpoint  allowed-ips  latest-hs  rx  tx  keepalive"""
    cached = _cache_get("awg")
    if cached:
        return cached
    rc, out, err = _docker_exec("amneziawg", ["awg", "show", "awg0", "dump"])
    peers = []
    iface_meta = {}
    handshake_count = 0
    if rc == 0 and out.strip():
        lines = out.strip().split("\n")
        # First line: <priv> <pub> <listen-port> <fwmark>
        first = lines[0].split("\t")
        if len(first) >= 3:
            iface_meta = {"public_key": first[1], "listen_port": first[2]}
        # Remaining lines: peer entries.
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) < 7:
                continue
            pub, _psk, endpoint, allowed, latest_hs, rx, tx = parts[:7]
            try:    latest_hs_int = int(latest_hs)
            except Exception: latest_hs_int = 0
            handshook = latest_hs_int > 0
            if handshook:
                handshake_count += 1
            age_sec = (time.time() - latest_hs_int) if latest_hs_int > 0 else None
            peers.append({
                "public_key":     pub[:16] + "…",
                "endpoint":       endpoint if endpoint != "(none)" else "",
                "allowed_ips":    allowed,
                "last_hs_unix":   latest_hs_int,
                "last_hs_age_s":  age_sec,
                "rx_bytes":       int(rx) if rx.isdigit() else 0,
                "tx_bytes":       int(tx) if tx.isdigit() else 0,
                "handshook":      handshook,
            })

    result = {
        "label":      "AmneziaWG (friend hub-spoke)",
        "summary":    f"{len(peers)} peer(s) · {handshake_count} with handshake",
        "iface":      iface_meta,
        "peers":      peers,
        "err":        (err or "")[:200] if rc != 0 else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("VPN dashboard starting on :8097")
    yield
    logger.info("VPN dashboard shutting down")


app = FastAPI(title="Sentinel VPN Dashboard", lifespan=lifespan)


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
    return JSONResponse(probe_amneziawg())


@app.get("/api/ark")
async def api_ark():
    return JSONResponse(probe_ark())


@app.get("/api/all")
async def api_all():
    cf = await probe_cloudflare()
    return {
        "cf":        cf,
        "headscale": probe_headscale(),
        "amneziawg": probe_amneziawg(),
        "ark":       probe_ark(),
    }


HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Sentinel VPN</title>
<style>
:root { --bg:#1c1c1e; --fg:#e8e8ea; --muted:#8e8e93; --section:#2c2c2e;
        --sep:#38383a; --pos:#34c759; --warn:#ffcc00; --neg:#ff453a;
        --accent:#2997ff; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { margin:0; padding:14px 12px 40px; background:var(--bg); color:var(--fg);
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
</style>
</head><body>

<header>
  <h1>🛡 Sentinel VPN</h1>
  <span class=refresh id=last-refresh>—</span>
</header>

<div id=routes>
  <div class=route><div class=empty><span class=spin></span> Loading…</div></div>
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
      <td><b>${esc(n.name)}</b></td>
      <td class=url>${esc(n.ipv4||'')}</td>
      <td>${esc(n.user||'')}</td>
      <td class=age>${n.online ? 'online' : (n.last_seen ? esc(n.last_seen.slice(0,16).replace('T',' ')) : '—')}</td>
    </tr>`).join('');
  const usersLine = (d.users||[]).map(u => esc(u.name||'?')).join(', ') || '<span class=age>(no users)</span>';
  return `
    <div class=route>
      <h2><span class="dot ${cls}"></span>🕸 ${esc(d.label)}<span class="tag ${cls}" style="margin-left:auto">${esc(d.summary)}</span></h2>
      <div class=sub>Users: ${usersLine}</div>
      ${d.err ? `<div class=empty style="color:var(--neg)">⚠ ${esc(d.err)}</div>` : ''}
      ${rows ? `<table>
        <thead><tr><th></th><th>Node</th><th>Tailnet IP</th><th>User</th><th>Last seen</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>` : '<div class=empty>No nodes registered yet. Run <code>tailscale up --login-server=https://headscale.your-domain.example.com</code> on a device.</div>'}
    </div>`;
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
      </table>` : '<div class=empty>No peers configured.</div>'}
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

async function refresh() {
  try {
    const r = await fetch('/api/all', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('routes').innerHTML =
      renderCF(d.cf) + renderHS(d.headscale) + renderAWG(d.amneziawg) + renderARK(d.ark);
    document.getElementById('last-refresh').textContent =
      'Refreshed ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('last-refresh').textContent = 'Refresh failed: ' + e;
  }
}

refresh();
setInterval(refresh, 30000);
</script>
</body></html>"""


@app.get("/vpn", response_class=HTMLResponse)
async def vpn_dashboard(request: Request):
    """VPN routes status page. Used to be at `/` — swapped to `/vpn` so the
    bare domain serves the Suite launcher (which the TWA targets at root)."""
    if not _is_authed(request):
        return RedirectResponse(url="/?next=/vpn", status_code=302)
    return HTMLResponse(HTML)


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
footer { color:var(--muted); font-size:10px; text-align:center; padding:8px 0 20px; margin-top:auto; }
</style></head><body>

<header>
  <div class=logo>🛡</div>
  <div>
    <h1>Sentinel Suite</h1>
    <div class=sub>Owner-only · your-domain.example.com</div>
  </div>
</header>

<div class=grid>
  <a class=tile href="https://sentinelfinance.your-domain.example.com/">
    <div class=ico>💰</div>
    <div class=name>Finance</div>
    <div class=desc>Net worth, statements, reconciliation</div>
  </a>
  <a class=tile href="https://media.your-domain.example.com/app">
    <div class=ico>📥</div>
    <div class=name>Media</div>
    <div class=desc>SMDL · downloads, scraper, live</div>
  </a>
  <a class=tile href="https://your-domain.example.com/">
    <div class=ico>🤖</div>
    <div class=name>Sentinel AI</div>
    <div class=desc>Agent, chat, memory</div>
  </a>
  <a class="tile accent" href="/vpn">
    <div class=ico>🌐</div>
    <div class=name>Network</div>
    <div class=desc>VPN routes · Headscale · AmneziaWG · WOL</div>
  </a>
</div>

<footer>Tap a tile to launch · By Azfar · Powered by Claude</footer>

</body></html>"""


# Minimal web app manifest — bubblewrap will read this at TWA-init time.
WEB_MANIFEST = {
    "name":             "Sentinel Suite",
    "short_name":       "Sentinel",
    "start_url":        "/",
    "scope":            "/",
    "display":          "standalone",
    "orientation":      "portrait",
    "theme_color":      "#1c1c1e",
    "background_color": "#1c1c1e",
    "description":      "Owner-only launcher for Sentinel Finance, SMDL Media, Sentinel AI, and Network ops.",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}


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


@app.get("/", response_class=HTMLResponse)
async def suite(request: Request, next: str = "/"):
    """Sentinel Suite launcher (4 tiles). Served at `/` so the TWA can target
    the bare domain (`https://suite.your-domain.example.com/`). VPN dashboard lives
    at `/vpn`. Unauthenticated requests get the setup form."""
    if not _is_authed(request):
        page = SETUP_HTML.replace("__NEXT__", next).replace("__ERR__", "")
        return HTMLResponse(page)
    return HTMLResponse(SUITE_HTML)


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
        page = SETUP_HTML.replace("__NEXT__", nxt).replace("__ERR__", "Invalid token")
        return HTMLResponse(page, status_code=401)
    resp = RedirectResponse(url=nxt, status_code=303)
    _set_session_cookie(resp, request)
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


@app.get("/icon-192.png")
async def icon_192():
    from fastapi.responses import Response
    return Response(content=_placeholder_icon(192), media_type="image/png")


@app.get("/icon-512.png")
async def icon_512():
    from fastapi.responses import Response
    return Response(content=_placeholder_icon(512), media_type="image/png")
