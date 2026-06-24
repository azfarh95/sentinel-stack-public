"""Single source of truth for filesystem paths used across the stack.

V6 prep: replaces hardcoded `C:\\Users\\azfar\\…` and
`\\\\wsl.localhost\\Ubuntu-24.04\\home\\azfar\\…` literals scattered through
bridge.py, watchdog.py, sync_lm_models.py, guest_caps.py.

This file lives at the repo root. Subdir callers do:

    import sys; from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _paths import OPENCLAW_JSON, COOKIES_DIR, LMS_EXE, REPO_ROOT

Override defaults via env vars at install time:
- SENTINEL_WSL_DISTRO    (default: Ubuntu-24.04)
- SENTINEL_WSL_USER      (default: $USERNAME — falls back to "azfar" only as last-ditch)
- SENTINEL_COOKIES_DIR   (default: $USERPROFILE\\YT-DLP\\cookies)
- SENTINEL_REPO_ROOT     (default: directory containing this file)
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Repo root (Windows-side) ─────────────────────────────────────────────────
# This file lives at <repo>/sentinel-miniapp-v2/_paths.py
REPO_ROOT = Path(os.environ.get("SENTINEL_REPO_ROOT") or
                 Path(__file__).resolve().parent)

SCRIPTS_DIR = REPO_ROOT / "scripts"
WATCHDOG_DIR = REPO_ROOT / "watchdog"
SENTINEL_DIR = REPO_ROOT / "sentinel-miniapp-v2"

# ── WSL paths via UNC \\wsl.localhost ────────────────────────────────────────
WSL_DISTRO = os.environ.get("SENTINEL_WSL_DISTRO", "Ubuntu-24.04")
WSL_USER   = os.environ.get("SENTINEL_WSL_USER",
                            os.environ.get("USERNAME", "azfar"))

WSL_HOME       = Path(rf"\\wsl.localhost\{WSL_DISTRO}\home\{WSL_USER}")
WSL_HOME_POSIX = f"/home/{WSL_USER}"   # for `wsl -- bash` invocations

OPENCLAW_DIR        = WSL_HOME / ".openclaw"
OPENCLAW_JSON       = OPENCLAW_DIR / "openclaw.json"
OPENCLAW_AGENT_DIR  = OPENCLAW_DIR / "agents" / "main" / "agent"
MODELS_JSON         = OPENCLAW_AGENT_DIR / "models.json"
AUTH_PROFILES_JSON  = OPENCLAW_AGENT_DIR / "auth-profiles.json"
SESSIONS_DIR        = OPENCLAW_DIR / "agents" / "main" / "sessions"
SESSIONS_JSON       = SESSIONS_DIR / "sessions.json"

OPENCLAW_CREDS_DIR  = OPENCLAW_DIR / "credentials"
TELEGRAM_PAIRING    = OPENCLAW_CREDS_DIR / "telegram-pairing.json"
TELEGRAM_ALLOWFROM  = OPENCLAW_CREDS_DIR / "telegram-default-allowFrom.json"

# OpenClaw npm-global (for restart / version-check / pairing-approve commands).
# Use explicit /home/{USER} path because some callers run with `wsl -u root`,
# where $HOME expands to /root — wrong location for OpenClaw's user-scope
# npm-global install.
OPENCLAW_NPM_PREFIX_BASH = f"{WSL_HOME_POSIX}/.npm-global"
OPENCLAW_NPM_BIN_BASH    = f"{OPENCLAW_NPM_PREFIX_BASH}/bin/openclaw"
OPENCLAW_NPM_PKG_PATH_BASH = f"{OPENCLAW_NPM_PREFIX_BASH}/lib/node_modules/openclaw/package.json"

# ── User-data dirs ───────────────────────────────────────────────────────────
USER_PROFILE = Path(os.environ.get("USERPROFILE", str(Path.home())))

LMS_EXE = USER_PROFILE / ".lmstudio" / "bin" / "lms.exe"

# Cookies dir — bridge.py also accepts a per-call override via config.json
COOKIES_DIR = Path(os.environ.get("SENTINEL_COOKIES_DIR")
                   or USER_PROFILE / "YT-DLP" / "cookies")

# Guest-usage SQLite DB (lives next to watchdog code for now)
GUEST_USAGE_DB = WATCHDOG_DIR / "guest_usage.db"

# ── Helpers ──────────────────────────────────────────────────────────────────

def wsl_systemctl_reload(service: str = "openclaw-gateway.service") -> list[str]:
    """Build the argv to hot-reload an OpenClaw systemd unit via SIGUSR1.
    Caller does subprocess.run(...) on the result."""
    return ["wsl", "-d", WSL_DISTRO, "-u", "root", "--", "bash", "-c",
            f"systemctl kill -s SIGUSR1 {service}"]


def wsl_bash(command: str, as_root: bool = False) -> list[str]:
    """Build argv for a bash command inside WSL. Use $HOME inside command for
    the user's home dir to keep it portable."""
    args = ["wsl", "-d", WSL_DISTRO]
    if as_root:
        args.extend(["-u", "root"])
    args.extend(["--", "bash", "-c", command])
    return args


# ── Diagnostic ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    print(json.dumps({
        "REPO_ROOT":          str(REPO_ROOT),
        "WSL_DISTRO":         WSL_DISTRO,
        "WSL_USER":           WSL_USER,
        "WSL_HOME":           str(WSL_HOME),
        "OPENCLAW_JSON":      str(OPENCLAW_JSON),
        "OPENCLAW_JSON_exists": OPENCLAW_JSON.exists(),
        "LMS_EXE":            str(LMS_EXE),
        "LMS_EXE_exists":     LMS_EXE.exists(),
        "COOKIES_DIR":        str(COOKIES_DIR),
        "COOKIES_DIR_exists": COOKIES_DIR.exists(),
    }, indent=2))
