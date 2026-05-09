# Sentinel Watchdog

A Telegram bot that monitors the Sentinel AI stack — Docker containers, processes, HTTP endpoints, and LM Studio — and alerts on state changes, auto-restarts failed services, and exposes a `/dashboard` shortcut to the Mini App.

**Bot handle:** `@YourWatchdogBot`  
Runs as a native Windows process via Task Scheduler. Fully independent of OpenClaw — survives OpenClaw crashes.

---

## Commands

| Command | Effect |
|---|---|
| `/status` | Full status snapshot: Docker containers / Processes / HTTP endpoints / LM Studio / Disk |
| `/restart [service]` | Restart menu (inline keyboard) or restart a named service directly |
| `/model` | Switch the active LM Studio model via inline keyboard |
| `/logs [container]` | Last N lines from any Docker container or OpenClaw `journalctl` |
| `/dashboard` | Sends an inline "Open Dashboard" button linking to the Sentinel Mini App |
| `/updates` | Check current vs latest versions of all 8 tracked components |
| `/power on` | Start the full AI stack sequence with progress updates |
| `/power off` | Stop the full AI stack |
| `/digest` | Request an immediate health digest |

---

## Alert monitor

- Checks every **1800 seconds** (30 minutes)
- Fires alerts only on **state transitions** — down→up or up→down; no repeated noise for a service that stays down
- Escalates if a service is still down after 10 minutes
- On startup: the first snapshot seeds `_last`; restart the watchdog **after** all services are confirmed up to avoid false "recovered" alerts on the next cycle

**Status icons:**

| Icon | Meaning |
|---|---|
| 🟢 | Healthy / running |
| ⚠️ | Warn — container starting, HTTP non-200, or bundle-MCP issue |
| 🔴 | Down — container stopped, port closed, or health check failed |

---

## Configuration

Copy `config.example.json` → `config.json` (gitignored) and fill in your values.

```json
{
  "owner_chat_id": 123456789,
  "wsl_distro": "Ubuntu-24.04",
  "openclaw_service": "openclaw-gateway.service",
  "compose_dir": "C:\\Users\\yourname\\metamcp-local",
  "compose_files": ["docker-compose.local.yml"],
  "openclaw_config": "\\\\wsl.localhost\\Ubuntu-24.04\\home\\yourname\\.openclaw\\openclaw.json",
  "alert_interval_seconds": 1800,
  "auto_restart": true,
  "digest_enabled": true,
  "digest_time": "08:00",
  "lm_studio_api_key": null,
  "dns_watch": ["yourdomain.xyz"],
  "github_repo": "youruser/your-repo",
  "github_pat": "ghp_YOUR_PERSONAL_ACCESS_TOKEN",
  "github_sync_interval": 300,
  "lm_studio_exe": null,
  "infer_bridge": null,
  "sentinel_bridge": null,
  "mini_app_url": "https://t.me/YourBotUsername/dashboard"
}
```

### Field reference

| Field | Description |
|---|---|
| `owner_chat_id` | Your Telegram user ID — only this ID can issue commands |
| `wsl_distro` | WSL2 distro name where OpenClaw runs |
| `openclaw_service` | systemd unit name for OpenClaw |
| `compose_dir` | Absolute path to the stack root (where docker-compose files live) |
| `compose_files` | List of compose files to manage |
| `openclaw_config` | UNC path to OpenClaw's `openclaw.json` inside WSL |
| `alert_interval_seconds` | How often the alert monitor polls (default: 1800) |
| `auto_restart` | Whether the watchdog auto-restarts failed services |
| `digest_enabled` | Send a daily status digest |
| `digest_time` | Time for the daily digest (24-hour, local time) |
| `lm_studio_api_key` | LM Studio API key — null to read from Credential Manager |
| `dns_watch` | Domains to check DNS resolution on |
| `github_repo` | `user/repo` for GitHub sync tracking |
| `github_pat` | GitHub PAT — null to read from Credential Manager |
| `github_sync_interval` | GitHub sync poll interval in seconds |
| `lm_studio_exe` | Full path to `LM Studio.exe` — null to auto-derive from `%LOCALAPPDATA%` |
| `infer_bridge` | Full path to `infer_bridge.py` — null to auto-derive from parent of `watchdog/` |
| `sentinel_bridge` | Full path to `bridge.py` — null to auto-derive from `sentinel-miniapp-v2/` |
| `mini_app_url` | `t.me` deep link used by the `/dashboard` command inline button |

`lm_studio_exe`, `infer_bridge`, and `sentinel_bridge` are auto-derived when null — override only when paths differ from the standard layout.

---

## Secrets management

Secrets live in **Windows Credential Manager**, never in `config.json`.

```powershell
# List all keys and their stored status
py store_secrets.py

# Store / rotate a secret
py store_secrets.py bot_token          <WATCHDOG_BOT_TOKEN>
py store_secrets.py lm_api_key         <LM_STUDIO_API_KEY>
py store_secrets.py github_pat         <GITHUB_PAT>
py store_secrets.py telegram_bot_token <MINIAPP_BOT_TOKEN>
py store_secrets.py mini_app_secret    <MINIAPP_SHARED_SECRET>
py store_secrets.py totp_secret        <TOTP_SEED>

# After rotating GitHub PAT — re-auth git CLI
gh auth login
```

Secrets are split across two Credential Manager services:

| Key | Service | Used by |
|---|---|---|
| `bot_token` | `sentinel-watchdog` | Watchdog bot |
| `lm_api_key` | `sentinel-watchdog` | LM Studio health check |
| `github_pat` | `sentinel-watchdog` | GitHub sync |
| `telegram_bot_token` | `sentinel-miniapp` | Mini App bridge |
| `mini_app_secret` | `sentinel-miniapp` | Mini App auth token |
| `totp_secret` | `sentinel-miniapp` | TOTP 2FA |

---

## Version tracking (`/updates`)

The watchdog tracks 8 components — current installed vs latest available:

| Component | Current source | Latest source |
|---|---|---|
| yt-dlp | `pip show` inside `ytdlp-mcp` container | GitHub releases API |
| gallery-dl | `pip show` inside `ytdlp-mcp` container | GitHub releases API |
| LibreTranslate | `/app/VERSION` inside `libretranslate` container | GitHub releases API |
| MetaMCP | OCI image label `org.opencontainers.image.version` | GitHub releases API |
| GitHub MCP | OCI image label `org.opencontainers.image.version` | GitHub releases API |
| OpenClaw | `npm list` in WSL Ubuntu-24.04 | npm registry |
| LM Studio | Windows exe `VersionInfo.ProductVersion` | GitHub releases API |
| Docker Desktop | Windows exe `VersionInfo.ProductVersion` | docs.docker.com/desktop/release-notes/ |

All version checks run in parallel via `ThreadPoolExecutor` to keep response time under 5 seconds.

---

## Auto-restart map

When `auto_restart: true`, the watchdog restarts failed services automatically:

| Service | Restart method |
|---|---|
| All Docker containers | `docker restart <name>` |
| Sentinel (OpenClaw) | `systemctl restart` via WSL |
| Infer Bridge | Relaunch `infer_bridge.py` |
| Sentinel Bridge | Relaunch `bridge.py` |
| Playwright proxy | `schtasks /Run` Task Scheduler task |

---

## Health check architecture

`get_health_snapshot()` runs all checks concurrently:

- **Docker containers** — `docker inspect` for health status; containers without a healthcheck fall back to running state
- **OpenClaw** — `systemctl is-active` via WSL
- **LM Studio** — TCP port 1234 reachability
- **HTTP endpoints** — `GET /health` or root URL for each MCP server

Cold-start time: ~2 seconds (parallelised from ~9 seconds sequential).

---

## Running

The watchdog is registered as a Windows Task Scheduler task (`Sentinel Watchdog`) set to run on login, auto-restart up to 5 times on failure.

```powershell
# Start manually
schtasks /Run /TN "Sentinel Watchdog"

# Stop
schtasks /End /TN "Sentinel Watchdog"

# Query status
schtasks /Query /TN "Sentinel Watchdog" /FO LIST
```

---

## Known Issues & Fixes

Tracked on GitHub Issues: https://github.com/YOUR_GITHUB_USERNAME/sentinel-watchdog/issues
