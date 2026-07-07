# AI Stack — Connectivity Scan Results

Last verified: 2026-05-01

## Results

| Component | Status | Endpoint | Detail |
|---|---|---|---|
| MetaMCP | ✅ healthy | `http://127.0.0.1:12008` | MCP initialize handshake OK; tools serving |
| Google Workspace MCP | ✅ healthy + authenticated | `http://127.0.0.1:8089` | `/health` → `{"status":"ok","authenticated":true}` |
| yt-dlp MCP | ✅ healthy | `http://127.0.0.1:8088` | `/health` → `{"status":"ok"}` |
| LM Studio | ✅ running | `http://127.0.0.1:1234` | Models: `google/gemma-4-e4b`, `qwen/qwen3.5-9b`, `nomic-embed` |
| LM Studio from WSL | ✅ reachable | `http://localhost:1234` | Confirmed via `curl` inside WSL2 |
| OpenClaw service | ✅ system service | `FragmentPath=/etc/systemd/system/` | PID stable 20+ min |
| OpenClaw port | ✅ bound | `127.0.0.1:18789` | `/health` → `{"ok":true,"status":"live"}` |
| OpenClaw → MetaMCP | ✅ connected | `localhost:12008` | 200+ tools registered in startup log |
| OpenClaw → LM Studio | ✅ connected | `localhost:1234` | `lmstudio/google/gemma-4-e4b` confirmed |
| Telegram bot | ✅ connected | `@YourSentinelBot` | Connected and receiving messages |
| Config watchdog | ✅ safe | `~/.openclaw/` | SHA256 matches `lastKnownGood` |
| User service conflict | ✅ none | — | User service `inactive` + `disabled` |
| Playwright MCP | ⚠️ not running | `127.0.0.1:8931` | Non-critical; auto-starts with LM Studio watcher |
| WhatsApp | ⚠️ cycling | — | Not configured; health-monitor restarts it periodically — expected |

## Observations

- **First message latency:** Gemma 4E4B takes 5–10 min for first inference after startup (model warm-up + 200+ tool context load). Subsequent messages are faster.
- **MetaMCP transport:** Must use `streamable-http` (not `sse` or `http`) in `openclaw.json`. SSE transport loses auth headers on the follow-up POST.
- **LM Studio bind address:** Binds only to `127.0.0.1:1234`. From WSL2 with `networkingMode=mirrored`, use `http://localhost:1234/v1` — the LAN IP (`192.168.50.74`) does not work.

## How to Re-run This Scan

```powershell
# Docker health
docker inspect metamcp google-workspace-mcp ytdlp-mcp --format "{{.Name}}: {{.State.Health.Status}}"

# Service ports
netstat -ano | findstr ":12008 :8089 :8088 :1234 :8931" | findstr LISTENING

# OpenClaw
wsl -d Ubuntu-24.04 -u root systemctl show openclaw-gateway.service --property=MainPID,ActiveState,FragmentPath

# LM Studio models from WSL
wsl -d Ubuntu-24.04 -u root curl -s -H "Authorization: Bearer <LMSTUDIO_APIKEY>" http://localhost:1234/v1/models

# MetaMCP MCP handshake
curl -s -H "Authorization: Bearer <METAMCP_TOKEN>" ^
     -H "Accept: application/json, text/event-stream" ^
     -X POST http://127.0.0.1:12008/metamcp/default/mcp ^
     -H "Content-Type: application/json" ^
     -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1\"}}}"
```
