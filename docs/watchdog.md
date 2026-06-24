# Sentinel Watchdog

A separate owner-only Telegram bot (`@YourWatchdogBot`) that runs as a native Windows process via Task Scheduler. Fully independent of OpenClaw — survives OpenClaw crashes.

- **Script:** `watchdog/watchdog.py`
- **Port:** 8099 (local HTTP status server)
- **Task Scheduler:** `Sentinel Watchdog` (runs as current user, auto-restarts 5×)
- **Config:** `watchdog/config.json` (gitignored) — see `watchdog/config.example.json`

---

## What it monitors

Every 30 minutes the watchdog checks:
- All Docker containers (via `docker inspect`)
- OpenClaw port `:18789` and systemd service state
- HTTP health endpoints for all MCP servers
- LM Studio port `:1234`
- DNS resolution for `your-domain.example.com` and `your-domain.example.com`

Alerts fire only on state **transitions** (up → down or down → up), not on every poll.

---

## Bot commands

### Sentinel Watchdog (`@YourWatchdogBot`)

| Command | Effect |
|---------|--------|
| `/status` | Full status: Docker / Processes / HTTP Endpoints / LM Studio / Disk |
| `/restart` | Restart menu — individual containers or full AI stack |
| `/model` | Switch LM Studio model (inline keyboard) |
| `/logs [container]` | Last N log lines from any container or OpenClaw journalctl |
| `/power on` | Start the full AI stack sequence with progress updates |
| `/power off` | Stop the full AI stack |
| `/digest` | Request an immediate health digest |

---

## Auto-restart map

Services the watchdog restarts automatically when they go down:

| Service | Restart method |
|---------|---------------|
| All Docker containers | `docker restart <name>` |
| Sentinel (OpenClaw) | `systemctl restart` via WSL |
| Infer Bridge | `py infer_bridge.py` |
| Sentinel Bridge | `py bridge.py` |
| Playwright proxy | Task Scheduler `schtasks /Run` |

If a service is still down 10 minutes after an automatic restart attempt, a critical alert is sent.

---

## DNS health checks

The watchdog checks DNS for both the apex and subdomain:

| Domain | Expected |
|--------|----------|
| `your-domain.example.com` | Resolves to a valid IP |
| `your-domain.example.com` | Resolves to Cloudflare anycast + HTTPS 200 |

**Open issue:** [#25](https://github.com/azfarh95/sentinel-stack-public/issues/25) — persistent DNS failures currently reported as "Still propagating" indefinitely. Fix: add failure counter threshold (>3 consecutive failures → critical alert).

---

## HTTP status API

Used by the Mini App bridge to populate the Watchdog Monitor screen.

| Endpoint | Response |
|----------|----------|
| `GET /status` | `{services: [...], endpoints: [...]}` |
| `GET /versions` | `{components: [{name, current, latest, needs_update}]}` |
| `POST /update` | Trigger a component update by `update_id` |

---

## Config fields (`watchdog/config.json`)

```json
{
  "owner_chat_id": 123456789,
  "wsl_distro": "Ubuntu-24.04",
  "openclaw_service": "openclaw-gateway.service",
  "compose_dir": "C:\\Users\\yourname\\metamcp-local",
  "compose_files": ["docker-compose.local.yml"],
  "alert_interval_seconds": 1800,
  "auto_restart": true,
  "digest_enabled": true,
  "digest_time": "08:00",
  "lm_studio_exe": null,
  "infer_bridge": null,
  "sentinel_bridge": null,
  "github_repo": "youruser/sentinel-stack",
  "github_sync_interval": 300
}
```

`lm_studio_exe`, `infer_bridge`, and `sentinel_bridge` default to auto-derived paths relative to `watchdog.py` when set to `null`.

---

## MCP server port map

| Service | Port |
|---------|------|
| MetaMCP | 12008 |
| Memory MCP | 8092 |
| Reminders MCP | 8087 |
| Google Workspace MCP | 8089 |
| Maps MCP | 8090 |
| GitHub MCP | 8091 |
| OneDrive MCP | 8093 |
| Translate MCP | 8094 |
| yt-dlp MCP | 8088 |
| Infer Bridge | 8095 |
| Mini App Bridge | 8098 |
| Watchdog | 8099 |
| LM Studio | 1234 |
