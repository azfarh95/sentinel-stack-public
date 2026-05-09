# AI Stack — Backup Guide

## Backup Size Summary

| Path | Size | Notes |
|---|---|---|
| `metamcp-local\scripts\` | ~16 KB | START/STOP/KEEPALIVE bat files |
| `metamcp-local\docker-compose.local.yml` | ~3 KB | Container definitions |
| `C:\Users\azfar\.wslconfig` | <1 KB | WSL2 VM settings |
| `C:\Users\azfar\.openclaw\` | ~1.1 MB | Windows-side stub config |
| `[WSL] ~/.openclaw/openclaw.json` | ~6 KB | **Primary config — critical** |
| `[WSL] ~/.openclaw/openclaw.json.last-good` | ~6 KB | Watchdog baseline |
| `[WSL] ~/.openclaw/credentials/` | ~20 KB | **OAuth tokens — critical** |
| `[WSL] ~/.openclaw/memory/` | ~108 KB | Agent memory |
| `[WSL] ~/.openclaw/agents/` | ~54 MB | Agent conversation history |
| `[WSL] ~/.openclaw/workspace/` | ~572 KB | Workspace files |
| `[WSL] ~/.openclaw/completions/` | ~540 KB | Completion cache |
| `[WSL] ~/.openclaw/tasks/` | ~308 KB | Task history |
| `[WSL] ~/.openclaw/media/` | ~16 MB | Media cache |
| `[WSL] /etc/systemd/system/openclaw-gateway.service` | ~1.3 KB | **System service unit — critical** |
| `[WSL] /etc/wsl.conf` | <1 KB | WSL distro settings |
| **Total (lean — no plugin deps)** | **~73 MB** | |
| `[WSL] ~/.openclaw/plugin-runtime-deps/` | ~2 GB | npm packages — **skip, regeneratable** |
| **Total (full)** | **~2.1 GB** | |

---

## What Is Critical vs Regeneratable

### Critical (back up — cannot be recreated without re-auth)
- `openclaw.json` + `openclaw.json.last-good` — all service URLs, API keys, bot tokens
- `openclaw/credentials/` — Google OAuth tokens; if lost, re-run Google auth flow
- `docker-compose.local.yml` — port mappings and container config
- `openclaw-gateway.service` — system service unit
- `.wslconfig` + `wsl.conf` — network and boot settings

### Useful (back up — saves time if lost)
- `agents/` — conversation history (54 MB)
- `memory/` — agent long-term memory
- `scripts/` — START/STOP/KEEPALIVE bat files
- `workspace/` — agent workspace files

### Skip (regeneratable)
- `plugin-runtime-deps/` — 2 GB npm packages; rebuilt automatically by OpenClaw on first start
  - Full backup stores this as `wsl-plugin-runtime-deps.tar.gz` (WSL tar used to avoid Windows MAX_PATH limits on deep npm cache paths)
- `completions/` — inference cache; rebuilt by use
- `media/` — re-downloadable

---

## Automated Backup Scripts

| Script | Schedule | Destination | Retention |
|---|---|---|---|
| `scripts\BACKUP_LEAN.ps1` | Daily 02:00 | `G:\AIStack-Backup\lean\YYYY-MM-DD\` | 14 days |
| `scripts\BACKUP_FULL.ps1` | Sunday 03:00 | `G:\AIStack-Backup\full\YYYY-MM-DD\` | 60 days (~8 weekly snapshots) |

### Registering the scheduled tasks (one-time setup)

Run **once** from an elevated PowerShell prompt:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& "C:\Users\azfar\metamcp-local\scripts\REGISTER_BACKUP_TASKS.ps1"
```

### Verifying tasks are registered

```powershell
Get-ScheduledTask -TaskName "AIStack-Backup-Lean","AIStack-Backup-Full" | Select TaskName, State, LastRunTime, NextRunTime
```

### Running a backup manually

```powershell
# Lean (any time):
& "C:\Users\azfar\metamcp-local\scripts\BACKUP_LEAN.ps1"

# Full (expect ~30-60 min for 2 GB):
& "C:\Users\azfar\metamcp-local\scripts\BACKUP_FULL.ps1"
```

### Backup logs

- `G:\AIStack-Backup\lean\backup.log`
- `G:\AIStack-Backup\full\backup.log`

---

## Restore Notes

### After fresh WSL install:
1. Copy `openclaw.json` to `/home/azfar/.openclaw/`
2. Copy `openclaw.json.last-good` alongside it
3. Restore `credentials/` folder
4. Copy `openclaw-gateway.service` to `/etc/systemd/system/`
5. Run: `systemctl daemon-reload && systemctl enable --now openclaw-gateway.service`
6. Restore `agents/` and `memory/` if needed

### After fresh Windows install:
1. Reinstall Docker Desktop, LM Studio, OpenClaw (WSL)
2. Restore `.wslconfig`
3. Restore `metamcp-local\` folder (scripts + compose)
4. Run `START_AI_STACK.bat`

### Re-authorising Google Workspace MCP (if credentials lost):
```bash
docker exec -it google-workspace-mcp sh
# Follow the OAuth flow printed to console
```
