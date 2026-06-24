# AI Stack — Architecture Overview

## Components

| Service | Where | Port | Managed By |
|---|---|---|---|
| MetaMCP | Docker container | 12008 | docker compose |
| Google Workspace MCP | Docker container | 8089 | docker compose |
| yt-dlp MCP | Docker container | 8088 | docker compose |
| Vaultwarden | Docker container | 8085 | docker compose |
| OpenClaw Gateway | WSL2 Ubuntu-24.04 | 18789 | systemd (system) |
| LM Studio API | Windows native | 1234 | Manual / app |
| Playwright MCP | Windows native | 8931 | LM Studio watcher |
| Telegram bot | External (Telegram) | — | @YourSentinelBot |

## Vaultwarden (self-hosted password manager)

- Image: `vaultwarden/server:latest`, container name `vaultwarden`
- Bound to **127.0.0.1:8085** only (loopback). Maps to internal port 80.
- Data: `C:\Users\azfar\metamcp-local\vaultwarden-data` (Windows host bind mount → `/data`)
- Admin URL: <http://127.0.0.1:8085/admin>
- Admin token: stored in WCM under service `sentinel-miniapp` user `vaultwarden_admin_token`
  (96-hex generated via `secrets.token_hex(48)`, sourced into `.env.local` by `scripts/sync_env_from_wcm.ps1` as `VAULTWARDEN_ADMIN_TOKEN`)
- Config: `SIGNUPS_ALLOWED=false`, `INVITATIONS_ALLOWED=false` — admin must create users via /admin
- Health: `curl http://127.0.0.1:8085/alive` returns 200; container exposes a built-in healthcheck
- Long-term role: home for stack secrets currently kept in Windows Credential Manager. Migration is a separate task — empty vault for now.

## Connection Map

```
Telegram
  └── OpenClaw (WSL2 :18789)
        ├── LM Studio (Windows localhost:1234)  ← inference
        └── MetaMCP (Docker localhost:12008)    ← tools
              ├── Google Workspace MCP (:8089)
              ├── yt-dlp MCP (:8088)
              └── Playwright MCP (:8931)  [optional]
```

## Key Config Files

| File | Purpose |
|---|---|
| `metamcp-local\docker-compose.local.yml` | Docker container definitions |
| `metamcp-local\scripts\START_AI_STACK.bat` | Startup script |
| `metamcp-local\scripts\STOP_AI_STACK.bat` | Shutdown script |
| `metamcp-local\scripts\WSL_KEEPALIVE.bat` | WSL2 keepalive (called by START) |
| `\\wsl$\Ubuntu-24.04\home\azfar\.openclaw\openclaw.json` | OpenClaw main config (WSL) |
| `\\wsl$\Ubuntu-24.04\etc\systemd\system\openclaw-gateway.service` | OpenClaw systemd unit |
| `C:\Users\azfar\.wslconfig` | WSL2 VM settings |
| `\\wsl$\Ubuntu-24.04\etc\wsl.conf` | WSL2 distro settings |

## OpenClaw Auth Tokens

| Token | Used For |
|---|---|
| MetaMCP Bearer (in openclaw.json) | OpenClaw → MetaMCP |
| OpenClaw gateway token (openclaw.json) | External → OpenClaw |
| LM Studio API key (openclaw.json) | OpenClaw → LM Studio |

> Full tokens are in the config files — not repeated here for security.
