"""Connectors hub — list all integrations + show live status.

Status is computed by probing each connector's most diagnostic endpoint
(or env var presence as a fallback). Page lives at /config/connectors.
"""
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")


async def _probe_firefly() -> dict:
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        return {"ok": False, "detail": "FIREFLY_PAT env var not set"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{FIREFLY_URL}/api/v1/about",
                            headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"})
        if r.status_code == 200:
            data = r.json().get("data", {})
            return {"ok": True, "detail": f"v{data.get('version','?')} · php {data.get('php_version','?')}"}
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:80]}


async def _probe_wise() -> dict:
    token = os.environ.get("WISE_API_TOKEN", "")
    if not token:
        return {"ok": False, "detail": "WISE_API_TOKEN env var not set"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.wise.com/v1/profiles",
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200 and isinstance(r.json(), list):
            profiles = r.json()
            return {"ok": True, "detail": f"{len(profiles)} profile(s) accessible"}
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:80]}


async def _probe_telegram(token_env: str, bot_label: str) -> dict:
    token = os.environ.get(token_env, "")
    if not token:
        return {"ok": False, "detail": f"{token_env} env var not set"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        if r.status_code == 200 and r.json().get("ok"):
            res = r.json()["result"]
            return {"ok": True, "detail": f"@{res.get('username')} · {res.get('first_name','')}"}
        return {"ok": False, "detail": "API rejected token"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:80]}


async def _probe_telegram_testbot() -> dict:
    """Probe via TESTBOT_TOKEN env if present (passed in from compose),
    else gracefully unavailable."""
    token = os.environ.get("TESTBOT_TOKEN", "")
    if not token:
        return {"ok": None, "detail": "TESTBOT_TOKEN not exposed to this container (live elsewhere — claude-assistant-testbot)"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        if r.status_code == 200 and r.json().get("ok"):
            res = r.json()["result"]
            return {"ok": True, "detail": f"@{res.get('username')} (Claude dev pings)"}
        return {"ok": False, "detail": "API rejected token"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:80]}


def _probe_google_token() -> dict:
    """Read the mounted google-workspace-mcp token file."""
    token_path = Path("/google-workspace-mcp/data/token.json")
    if not token_path.exists():
        return {"ok": None, "detail": "token.json not mounted (read-only) into this container"}
    try:
        data = json.loads(token_path.read_text())
        scopes = data.get("scopes", data.get("scope", []))
        if isinstance(scopes, str):
            scopes = scopes.split()
        scope_short = [s.split("/")[-1] for s in scopes]
        return {"ok": True, "detail": f"{len(scopes)} scopes: {', '.join(scope_short[:3])}"
                + (f" +{len(scopes)-3}" if len(scopes) > 3 else "")}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:80]}


def _probe_container(container_name: str, friendly_detail: str) -> dict:
    """Best-effort: try docker CLI. If unavailable (running inside container without
    docker.sock), just say so — the user knows it's running if the page loads."""
    try:
        r = subprocess.run(["docker", "inspect", "--format",
                            "{{.State.Status}}|{{.State.Health.Status}}", container_name],
                           capture_output=True, text=True, timeout=4)
        if r.returncode != 0:
            return {"ok": None, "detail": f"docker CLI unavailable · {friendly_detail}"}
        parts = r.stdout.strip().split("|")
        if not parts or not parts[0]:
            return {"ok": False, "detail": "container not found"}
        state = parts[0]
        health = parts[1] if len(parts) > 1 else ""
        ok = state == "running" and (not health or health == "healthy")
        return {"ok": ok, "detail": f"{state}{(' · ' + health) if health else ''} · {friendly_detail}"}
    except FileNotFoundError:
        return {"ok": None, "detail": f"docker CLI unavailable · {friendly_detail}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:80]}


async def check_all() -> list[dict]:
    """Probe every connector. Returns list of dicts: {key, name, group, status, detail, last_synced}."""
    out = []

    out.append({
        "key": "firefly", "name": "Firefly III", "group": "Data store",
        "icon": "📊", "purpose": "Source of truth for accounts, transactions, categories",
        **(await _probe_firefly()),
    })
    out.append({
        "key": "wise", "name": "Wise", "group": "Accounts",
        "icon": "💱", "purpose": "Multi-currency cash account · auto-synced daily 06:30",
        **(await _probe_wise()),
    })
    out.append({
        "key": "google-mcp", "name": "Google Workspace", "group": "Integrations",
        "icon": "📧", "purpose": "Gmail (dividend parsing) + Calendar (insurance reminders) + Drive",
        **(_probe_google_token()),
    })
    out.append({
        "key": "telegram-sentinel", "name": "YourSentinelBot", "group": "Notifications",
        "icon": "📣", "purpose": "User-facing Sentinel commands (/wallet_snapshot, /balance, /cashflow)",
        **(await _probe_telegram("TELEGRAM_BOT_TOKEN", "YourSentinelBot")),
    })
    out.append({
        "key": "telegram-testbot", "name": "Sentinel_claude_testbot_bot", "group": "Notifications",
        "icon": "🤖", "purpose": "Claude → user development pings + file delivery",
        **(await _probe_telegram_testbot()),
    })
    out.append({
        "key": "onedrive-mcp", "name": "OneDrive (MCP)", "group": "Integrations",
        "icon": "☁️", "purpose": "Sentinel Finance dropfolder · planned: auto-parse statements",
        **(_probe_container("onedrive-mcp", "uploads / downloads via MCP")),
    })
    out.append({
        "key": "cloudflared", "name": "Cloudflare Tunnel", "group": "Network",
        "icon": "🌐", "purpose": "Public HTTPS for sentinelfinance.your-domain.example.com + firefly.your-domain.example.com",
        # cloudflared runs on the Windows host, not Docker. Best-effort check:
        **(_probe_container("metamcp", "tunnel runs on host via Windows service")),
    })
    out.append({
        "key": "moralis", "name": "Moralis Web3", "group": "Crypto",
        "icon": "🪙", "purpose": "Multi-chain wallet snapshots (ETH/BSC/Polygon/Arbitrum/Base/Avalanche/Cronos)",
        "ok": bool(os.environ.get("MORALIS_API_KEY")),
        "detail": "Key present" if os.environ.get("MORALIS_API_KEY") else "MORALIS_API_KEY env var not set",
    })

    # ILP + CPF variance probes — surface drift between Firefly balance and
    # computed (units × NAV) so the user spots stale NAVs without opening
    # the drill page. >3% drift = red, >0.5% = yellow, else green.
    out.append(await _probe_ilp_variance())
    out.append(await _probe_cpf_variance())

    return out


def _variance_status(pct: float | None, label: str, computed: float, live: float) -> tuple[bool | None, str]:
    """Convert a percent drift into (ok, detail) for the connectors hub."""
    if pct is None:
        return None, f"{label}: no computed value (funds.yaml unmapped)"
    drift = computed - live
    if abs(pct) > 3.0:
        return False, f"{label}: {drift:+,.2f} SGD ({pct:+.2f}%) — refresh NAVs"
    if abs(pct) > 0.5:
        return None, f"{label}: {drift:+,.2f} SGD ({pct:+.2f}%) — minor drift"
    return True, f"{label}: {drift:+,.2f} SGD ({pct:+.2f}%) — in sync"


async def _probe_ilp_variance() -> dict:
    """Run the ILP drill builder, surface aggregate variance %."""
    try:
        from . import drill as _drill
        d = await _drill.build_ilp_drill()
        live = d.get("grand_firefly_sgd", 0.0)
        computed = d.get("grand_computed_sgd", 0.0)
        pct = (computed - live) / live * 100 if live else None
        ok, detail = _variance_status(pct, "ILP", computed, live)
        return {
            "key": "ilp_variance", "name": "ILP units × NAV reconciliation",
            "group": "Reconciliation", "icon": "📈",
            "purpose": "Drift between Firefly cash value and computed (sum units × NAV) across all ILP policies",
            "ok": ok, "detail": detail,
        }
    except Exception as e:
        return {"key": "ilp_variance", "name": "ILP units × NAV reconciliation",
                "group": "Reconciliation", "icon": "📈",
                "purpose": "Drift between Firefly cash value and computed",
                "ok": False, "detail": f"probe failed: {str(e)[:120]}"}


async def _probe_cpf_variance() -> dict:
    """Run the CPF drill builder, surface CPF IS variance %."""
    try:
        from . import drill as _drill
        d = await _drill.build_cpf_drill()
        is_acct = d.get("is_account") or {}
        live = float(is_acct.get("sgd", 0.0))
        computed = float(d.get("is_computed_sgd", 0.0))
        if not is_acct or computed == 0.0:
            return {"key": "cpf_variance", "name": "CPF-IS units × NAV reconciliation",
                    "group": "Reconciliation", "icon": "🏦",
                    "purpose": "Drift between Firefly CPF IS balance and computed",
                    "ok": None, "detail": "no CPF-IS fund mapping in funds.yaml"}
        pct = (computed - live) / live * 100 if live else None
        ok, detail = _variance_status(pct, "CPF IS", computed, live)
        return {"key": "cpf_variance", "name": "CPF-IS units × NAV reconciliation",
                "group": "Reconciliation", "icon": "🏦",
                "purpose": "Drift between Firefly CPF IS balance and computed (sum units × NAV)",
                "ok": ok, "detail": detail}
    except Exception as e:
        return {"key": "cpf_variance", "name": "CPF-IS units × NAV reconciliation",
                "group": "Reconciliation", "icon": "🏦",
                "purpose": "Drift between Firefly CPF IS balance and computed",
                "ok": False, "detail": f"probe failed: {str(e)[:120]}"}
