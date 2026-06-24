"""Layered secrets backend — Mini-app Config 'Keys' card.

Each secret has:
  - storage   : 'wcm', 'openclaw', or 'wcm+openclaw' (mirrored)
  - retrieval : how the user gets a new value (URL + steps)
  - revoke    : reminder of what to revoke after issuing the new one
  - reload    : list of services to restart after rotation
  - test      : optional smoke-test name (resolved by smoke_tests dict)

Read state never returns the value — only metadata. Writes go through
keyring + jq+SIGUSR1 depending on storage. This module is intentionally
small and testable; the Vaultwarden swap-in (V2) only replaces the
WCM functions.
"""
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import keyring

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

# ── Per-secret declarative map ────────────────────────────────────────────────
# key = stable name used by the API; UI reads `label` for display.

SECRETS = {
    "tavily": {
        "label":    "Tavily web search",
        "category": "Cloud APIs",
        "storage":  "openclaw",
        "openclaw_path": ".plugins.entries.tavily.config.webSearch.apiKey",
        "retrieval": [
            "Open https://app.tavily.com/dashboard",
            "API Keys → click Generate new key",
            "Copy the tvly-... key (it's shown only once)",
        ],
        "revoke": "After saving here, return to the Tavily dashboard and Revoke the previous key.",
        "reload": ["openclaw-sigusr1"],
        "test":   "tavily_search",
        "value_prefix": "tvly-",
    },
    "azure-speech": {
        "label":    "Azure Speech",
        "category": "Cloud APIs",
        "storage":  "openclaw",
        "openclaw_path": ".talk.providers.azure-speech.apiKey",
        "retrieval": [
            "Open https://portal.azure.com",
            "Find your Speech resource → Resource Management → Keys and Endpoint",
            "Copy Key 1 (or rotate Key 2 first if Key 1 is in use)",
        ],
        "revoke": "After saving here, click Regenerate next to the OLD key in the Azure portal so it can no longer be used.",
        "reload": ["openclaw-sigusr1"],
        "test":   None,
    },
    "lmstudio": {
        "label":    "LM Studio API key",
        "category": "Cloud APIs",
        "storage":  "openclaw",
        "openclaw_path": ".models.providers.lmstudio.apiKey",
        "retrieval": [
            "Open LM Studio → Settings (gear icon)",
            "Find Server / API Key section",
            "Copy or regenerate the key",
            "(If LM Studio doesn't expose a key UI, pick any random hex string and paste the same value into both LM Studio and here)",
        ],
        "revoke": "If LM Studio exposes a key, regenerate the old one. If you used a self-picked random string, no revoke needed — old value just stops being accepted.",
        "reload": ["openclaw-sigusr1"],
        "test":   None,
    },
    "github-pat": {
        "label":    "GitHub Personal Access Token",
        "category": "Cloud APIs",
        "storage":  "wcm",
        "wcm_service": "sentinel-miniapp",
        "wcm_user":    "github_pat",
        "retrieval": [
            "Open https://github.com/settings/tokens",
            "Generate new token (classic)",
            "Required scopes: repo, workflow, read:org",
            "Copy the ghp_... token (shown only once)",
        ],
        "revoke": "After saving, go back to https://github.com/settings/tokens and click Delete on the previous token.",
        "reload": ["docker-restart:github-mcp"],
        "test":   "github_whoami",
        "value_prefix": "ghp_",
    },
    "telegram-ai": {
        "label":    "Sentinel bot token (@YourSentinelBot)",
        "category": "Bots",
        "storage":  "wcm+openclaw",
        "wcm_service": "sentinel-miniapp",
        "wcm_user":    "telegram_bot_token",
        "openclaw_path": ".channels.telegram.botToken",
        "retrieval": [
            "Open Telegram, message @BotFather",
            "Send /token → select @YourSentinelBot",
            "If you want a fresh token: /revoke → select @YourSentinelBot → /token",
            "Copy the new token",
        ],
        "revoke": "BotFather automatically invalidates the old token when you /revoke. No extra step needed.",
        "reload": ["openclaw-sigusr1"],
        "test":   "telegram_getme",
    },
    "telegram-watchdog": {
        "label":    "Watchdog bot token (@YourWatchdogBot)",
        "category": "Bots",
        "storage":  "wcm",
        "wcm_service": "sentinel-watchdog",
        "wcm_user":    "bot_token",
        "retrieval": [
            "Telegram → @BotFather → /token",
            "Select @YourWatchdogBot",
            "Or /revoke first if rotating away from a leaked token",
        ],
        "revoke": "BotFather invalidates the old token automatically on /revoke.",
        "reload": ["restart:watchdog"],
        "test":   "telegram_getme_watchdog",
    },
    "metamcp-bearer": {
        "label":    "MetaMCP bearer token",
        "category": "Internal",
        "storage":  "wcm+openclaw",
        "wcm_service": "sentinel-miniapp",
        "wcm_user":    "metamcp_bearer_token",
        "openclaw_path": ".mcp.servers.metamcp.headers.Authorization",
        "openclaw_value_prefix": "Bearer ",  # prepended when writing to openclaw.json
        "retrieval": [
            "Self-issued — no external portal.",
            "Generate with: python -c \"import secrets; print(secrets.token_hex(24))\"",
            "Or click Auto-generate below to let the server pick one.",
        ],
        "revoke": "MetaMCP only checks the current token — once you save the new one, the old one stops working immediately. No external revoke step.",
        "reload": ["openclaw-sigusr1", "restart:bridge"],
        "test":   None,
        "autogen": True,  # supports server-side generation if value omitted
    },
    "gateway-auth": {
        "label":    "OpenClaw gateway auth token",
        "category": "Internal",
        "storage":  "openclaw",
        "openclaw_path": ".gateway.auth.token",
        "retrieval": [
            "Self-issued — no external portal.",
            "Click Auto-generate below for a fresh 48-character hex token.",
        ],
        "revoke": "Old token stops working when the new one is saved. No external revoke needed.",
        "reload": ["openclaw-sigusr1"],
        "test":   None,
        "autogen": True,
    },
    "telegram-testbot": {
        "label":    "Testbot token (@SentinelClaudeAssistantBot)",
        "category": "Bots",
        "storage":  "envfile",
        "envfile_path":  r"C:\Users\azfar\.claude\projects\Projects-Proposal-WIP\V4\ClaudeAssistant\.env.testenv",
        "envfile_key":   "TESTBOT_TOKEN",
        "retrieval": [
            "Telegram → @BotFather → /token",
            "Select @SentinelClaudeAssistantBot",
            "Or /revoke first if rotating from a leaked token",
        ],
        "revoke": "BotFather invalidates the old token automatically on /revoke.",
        "reload": ["restart:testbot-container"],
        "test":   "telegram_getme",
    },
    "totp": {
        "label":    "Mini-app TOTP secret (this app's 2FA)",
        "category": "Internal",
        "storage":  "wcm",
        "wcm_service": "sentinel-miniapp",
        "wcm_user":    "totp_secret",
        "retrieval": [
            "Self-issued — no external portal.",
            "Click Reset & new QR below.",
            "Bridge regenerates a new secret and surfaces a fresh QR at totp_setup.html.",
            "Open that page, scan the QR with your authenticator app, then re-login.",
        ],
        "revoke": "Old TOTP secret is overwritten on reset. Existing authenticator entry stops working — that's why you scan a new QR.",
        "reload": ["restart:bridge"],
        "test":   None,
        "regen_only": True,  # special — value field hidden, only Reset action shown
    },
    "telethon-session": {
        "label":    "Telethon session (mini-app composer)",
        "category": "Interactive",
        "storage":  "wcm",
        "wcm_service": "sentinel-miniapp",
        "wcm_user":    "telethon_session",
        "retrieval": [
            "This needs an interactive Python flow (phone + SMS code).",
            "Run in a Python REPL on this machine:",
            "  from telethon import TelegramClient",
            "  from telethon.sessions import StringSession",
            "  import keyring",
            "  api_id   = int(keyring.get_password('sentinel-miniapp', 'telethon_api_id'))",
            "  api_hash = keyring.get_password('sentinel-miniapp', 'telethon_api_hash')",
            "  with TelegramClient(StringSession(), api_id, api_hash) as c:",
            "      c.start()  # prompts for phone, then SMS code",
            "      print(c.session.save())  # COPY THIS, do not share",
            "Then paste the new session string into the value field below.",
        ],
        "revoke": "Telegram invalidates the old session as soon as a new login completes — no extra revoke step.",
        "reload": ["restart:bridge"],
        "test":   None,
    },
    "google-oauth": {
        "label":    "Google Workspace (Gmail + Calendar + Drive)",
        "category": "Interactive",
        "storage":  "tokenfile",
        "tokenfile_path": "/data/token.json",  # inside google-workspace-mcp container
        "retrieval": [
            "Browser-flow only — can't paste a value here.",
            "Visit https://myaccount.google.com/permissions",
            "Find 'Sentinel Google Workspace MCP' → Remove access",
            "Then open http://localhost:8089/oauth in browser → sign in → grant scopes",
            "New refresh tokens are auto-saved to the container volume.",
        ],
        "revoke": "Removing access in Google's permissions panel revokes the OLD refresh token. Without that step, the old token stays valid.",
        "reload": [],
        "test":   None,
        "external_oauth": "http://localhost:8089/oauth",  # UI shows 'Open re-auth URL' button instead of paste form
    },
    "microsoft-oauth": {
        "label":    "OneDrive (Microsoft 365)",
        "category": "Interactive",
        "storage":  "tokenfile",
        "tokenfile_path": "/data/token.json",  # inside onedrive-mcp container
        "retrieval": [
            "Browser-flow only — can't paste a value here.",
            "Visit https://account.microsoft.com/privacy/app-access",
            "Revoke the OneDrive MCP app",
            "Then open http://localhost:8093/auth in browser → sign in → grant scopes",
            "New refresh tokens are auto-saved to the container volume.",
        ],
        "revoke": "Revoking the app at account.microsoft.com invalidates the old refresh token. Without that step, the old token stays valid.",
        "reload": [],
        "test":   None,
        "external_oauth": "http://localhost:8093/auth",
    },
    "cloudflare-tunnel": {
        "label":    "Cloudflare tunnel (sentinel.your-domain)",
        "category": "Interactive",
        "storage":  "external",
        "retrieval": [
            "Multi-step CLI dance — use cloudflared:",
            "  1. cloudflared tunnel delete sentinel",
            "  2. cloudflared tunnel create sentinel-2",
            "  3. cloudflared tunnel route dns sentinel-2 sentinel.your-domain.example.com",
            "  4. Update C:\\Users\\azfar\\.cloudflared\\config.yml to point at the new tunnel UUID",
            "  5. Restart cloudflared service",
        ],
        "revoke": "Step 1 (tunnel delete) IS the revoke. The deleted tunnel can no longer accept connections.",
        "reload": ["restart:cloudflared"],
        "test":   None,
        "instructions_only": True,  # UI shows steps only, no rotate button
    },
}

# ── Storage primitives ───────────────────────────────────────────────────────

def _wcm_get(service: str, user: str) -> Optional[str]:
    try:
        return keyring.get_password(service, user)
    except Exception:
        return None


def _wcm_set(service: str, user: str, value: str) -> None:
    keyring.set_password(service, user, value)


def _openclaw_path() -> str:
    """Path to openclaw.json inside WSL (we read/write via wsl + jq)."""
    return "~/.openclaw/openclaw.json"


def _openclaw_get(jq_path: str) -> Optional[str]:
    """Read a string from openclaw.json via wsl + jq. Returns None on missing."""
    try:
        cmd = f"jq -r '{jq_path} // empty' {_openclaw_path()}"
        r = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "-e", "bash", "-c", cmd],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW,
        )
        out = r.stdout.strip()
        return out or None
    except Exception:
        return None


def _openclaw_set(jq_path: str, value: str) -> bool:
    """Write a string into openclaw.json atomically via wsl + jq."""
    # Single-line bash; CRLF-safe via && chain.
    safe_value = value.replace("'", "'\\''")
    cmd = (
        f"jq --arg v '{safe_value}' '{jq_path} = $v' {_openclaw_path()} > /tmp/oc-secret.json "
        f"&& [ -s /tmp/oc-secret.json ] && mv /tmp/oc-secret.json {_openclaw_path()} && echo OK"
    )
    try:
        r = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "-e", "bash", "-c", cmd],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        )
        return "OK" in r.stdout
    except Exception:
        return False


# ── Reload primitives ────────────────────────────────────────────────────────

def _reload_openclaw_sigusr1() -> bool:
    try:
        subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
             "systemctl kill -s SIGUSR1 openclaw-gateway.service"],
            capture_output=True, timeout=5, creationflags=_NO_WINDOW,
        )
        return True
    except Exception:
        return False


def _docker_restart(container: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "restart", container],
            capture_output=True, timeout=30, creationflags=_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── Smoke tests ──────────────────────────────────────────────────────────────

def _smoke_tavily(value: str) -> dict:
    try:
        body = json.dumps({"api_key": value, "query": "test", "max_results": 1}).encode()
        req  = urllib.request.Request(
            "https://api.tavily.com/search",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return {"ok": bool(data.get("results")), "detail": "search returned results"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:100]}


def _smoke_github(value: str) -> dict:
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {value}", "User-Agent": "sentinel-rotate"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return {"ok": True, "detail": f"authenticated as {data.get('login', 'unknown')}"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:100]}


def _smoke_telegram_getme(value: str) -> dict:
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{value}/getMe", timeout=8
        ) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return {"ok": True, "detail": f"@{data['result']['username']}"}
            return {"ok": False, "detail": data.get("description", "unknown error")}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:100]}


SMOKE_TESTS = {
    "tavily_search":          _smoke_tavily,
    "github_whoami":          _smoke_github,
    "telegram_getme":         _smoke_telegram_getme,
    "telegram_getme_watchdog": _smoke_telegram_getme,
}


# ── Public API ───────────────────────────────────────────────────────────────

def list_secrets() -> list:
    """Return metadata for every secret. Never includes the value itself."""
    out = []
    for name, spec in SECRETS.items():
        status = _status_of(name, spec)
        out.append({
            "name":              name,
            "label":             spec["label"],
            "category":          spec["category"],
            "storage":           spec["storage"],
            "status":            status,
            "retrieval":         spec["retrieval"],
            "revoke":            spec["revoke"],
            "has_test":          bool(spec.get("test")),
            "autogen":           bool(spec.get("autogen")),
            "regen_only":        bool(spec.get("regen_only")),
            "external_oauth":    spec.get("external_oauth"),
            "instructions_only": bool(spec.get("instructions_only")),
        })
    return out


def _envfile_get(path: str, key: str) -> Optional[str]:
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip()
                    return val if val else None
    except Exception:
        pass
    return None


def _envfile_set(path: str, key: str, value: str) -> bool:
    try:
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            content = f.read()
        import re
        new_line = f"{key}={value}"
        if re.search(rf"(?m)^{re.escape(key)}=.*$", content):
            content = re.sub(rf"(?m)^{re.escape(key)}=.*$", new_line, content)
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += new_line + "\n"
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return True
    except Exception:
        return False


def _status_of(name: str, spec: dict) -> str:
    """Return a status string: 'set', 'missing', 'external', 'oauth-only'."""
    storage = spec["storage"]
    if storage == "external":
        return "external"  # Cloudflare etc — can't introspect
    if storage == "tokenfile":
        return "oauth-only"  # OAuth refresh token — handled out-of-band
    if "wcm" in storage:
        v = _wcm_get(spec["wcm_service"], spec["wcm_user"])
        if not v:
            return "missing"
    if "openclaw" in storage:
        v = _openclaw_get(spec["openclaw_path"])
        if not v:
            return "missing"
    if storage == "envfile":
        v = _envfile_get(spec["envfile_path"], spec["envfile_key"])
        if not v:
            return "missing"
    return "set"


def rotate(name: str, value: Optional[str], regen: bool = False) -> dict:
    """Rotate a single secret. Returns {ok, detail, smoke_test, restarted}.
    `value` may be None for autogen-capable secrets.
    `regen=True` triggers regen-only secrets (TOTP) — value is ignored, secret is deleted.
    """
    if name not in SECRETS:
        return {"ok": False, "detail": "unknown secret"}
    spec = SECRETS[name]

    # Regen-only path (TOTP — bridge regenerates on next start)
    if spec.get("regen_only"):
        if not regen:
            return {"ok": False, "detail": "this secret is regen-only — pass regen:true"}
        try:
            keyring.delete_password(spec["wcm_service"], spec["wcm_user"])
        except Exception:
            pass  # idempotent
        return {
            "ok": True,
            "detail": "secret cleared — bridge will regenerate on next start",
            "smoke_test": None,
            "restarted":  [],
            "manual_restart_needed": ["bridge"],
        }

    # Instructions-only / external secrets can't be rotated via API
    if spec.get("instructions_only"):
        return {"ok": False, "detail": "this secret is rotated externally — follow the instructions"}

    # Autogen handling
    if value is None or value == "":
        if not spec.get("autogen"):
            return {"ok": False, "detail": "value required (this secret is not auto-generatable)"}
        import secrets as _secrets
        value = _secrets.token_hex(24)

    if not isinstance(value, str) or len(value) < 8:
        return {"ok": False, "detail": "value too short"}

    # Storage write
    storage = spec["storage"]
    if storage in ("external", "tokenfile"):
        return {"ok": False, "detail": "this secret is interactive — cannot rotate via paste"}
    if "wcm" in storage:
        try:
            _wcm_set(spec["wcm_service"], spec["wcm_user"], value)
        except Exception as e:
            return {"ok": False, "detail": f"WCM write failed: {e}"}
    if "openclaw" in storage:
        oc_value = spec.get("openclaw_value_prefix", "") + value
        if not _openclaw_set(spec["openclaw_path"], oc_value):
            return {"ok": False, "detail": "openclaw.json write failed"}
    if storage == "envfile":
        if not _envfile_set(spec["envfile_path"], spec["envfile_key"], value):
            return {"ok": False, "detail": "envfile write failed (file missing or unwritable)"}

    # Reload
    restarted = []
    for action in spec.get("reload", []):
        if action == "openclaw-sigusr1":
            if _reload_openclaw_sigusr1():
                restarted.append("openclaw-gateway")
        elif action.startswith("docker-restart:"):
            container = action.split(":", 1)[1]
            if _docker_restart(container):
                restarted.append(f"docker:{container}")
        # restart:watchdog and restart:bridge are intentionally not auto-applied
        # — those would kill THIS process. Surfaced in UI as a manual step.

    # Smoke test
    smoke = None
    test_name = spec.get("test")
    if test_name and test_name in SMOKE_TESTS:
        smoke = SMOKE_TESTS[test_name](value)

    return {
        "ok": True,
        "detail": "rotated",
        "smoke_test": smoke,
        "restarted":  restarted,
        "manual_restart_needed": [a.split(":", 1)[1] for a in spec.get("reload", []) if a.startswith("restart:")],
    }


def smoke_test_only(name: str) -> dict:
    """Run the smoke test against the CURRENT stored value, no rotation."""
    if name not in SECRETS:
        return {"ok": False, "detail": "unknown secret"}
    spec = SECRETS[name]
    test_name = spec.get("test")
    if not test_name or test_name not in SMOKE_TESTS:
        return {"ok": False, "detail": "no smoke test for this secret"}

    storage = spec["storage"]
    value = None
    if "wcm" in storage:
        value = _wcm_get(spec["wcm_service"], spec["wcm_user"])
    if not value and "openclaw" in storage:
        value = _openclaw_get(spec["openclaw_path"])
        if value and spec.get("openclaw_value_prefix"):
            value = value[len(spec["openclaw_value_prefix"]):]
    if not value:
        return {"ok": False, "detail": "no stored value"}

    return SMOKE_TESTS[test_name](value)
