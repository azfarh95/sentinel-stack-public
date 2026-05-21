# AI Stack — Troubleshooting Guide

## Quick Diagnostics

Run these in order to locate a fault:

```powershell
# 1. Docker containers
docker inspect metamcp google-workspace-mcp ytdlp-mcp --format "{{.Name}}: {{.State.Health.Status}}"

# 2. OpenClaw service (must say "active")
wsl -d Ubuntu-24.04 -u root systemctl is-active openclaw-gateway.service

# 3. LM Studio port
netstat -ano | findstr ":1234 " | findstr LISTENING

# 4. OpenClaw recent logs (filtered)
wsl -d Ubuntu-24.04 -u root journalctl -u openclaw-gateway.service --no-pager -n 20
```

---

## Issue 1: OpenClaw cycles every ~15 seconds

**Symptom:** `journalctl` shows repeated `Started / Stopping / Stopped` for `openclaw-gateway.service` every 15–17 seconds, with different `systemd[NNN]` PIDs each time.

**Root Cause:** OpenClaw was running as a *user* service under `user@1000.service`. WSL2 restarts `user@1000.service` every time any `wsl.exe` session exits (including one-shot `wsl -d Ubuntu-24.04 -u azfar -- bash -c "..."` diagnostic commands). Each restart kills OpenClaw.

**Fix:** Convert to a *system* service so it is managed by `systemd[1]` (unaffected by user session lifecycle):

```bash
# Inside WSL as root:
# 1. Disable user service
systemctl --user -M azfar@ stop openclaw-gateway.service
systemctl --user -M azfar@ disable openclaw-gateway.service

# 2. Create system service at /etc/systemd/system/openclaw-gateway.service
#    (copy from Maintenance/openclaw-gateway.service.bak in this folder)

# 3. Enable and start
systemctl daemon-reload
systemctl enable openclaw-gateway.service
systemctl start openclaw-gateway.service
```

**Verify:** `FragmentPath` must point to `/etc/systemd/system/`, NOT `/home/azfar/.config/systemd/user/`:
```bash
wsl -d Ubuntu-24.04 -u root systemctl show openclaw-gateway.service --property=FragmentPath
```

---

## Issue 2: WSL2 distro shuts down, killing all services

**Symptom:** Journal shows `systemd-logind: The system will power off now!` followed by a restart. OpenClaw PID changes. Preceded by `Operation canceled @p9io.cpp:258 (AcceptAsync)`.

**Root Cause:** WSL2 sends an ACPI power-off signal to Ubuntu-24.04 when all `wsl.exe` processes exit. Even with `systemd=true` and `Linger=yes`, an *active wsl session* must be maintained to prevent shutdown.

**Fix:** The START script launches `scripts\WSL_KEEPALIVE.bat` as a background minimised window. This runs `wsl -d Ubuntu-24.04 -u root sleep infinity` in a loop, keeping the distro alive. The STOP script kills it.

**Key detail:** The keepalive command must NOT use nested quotes (`bash -c "..."`) inside a `.bat` file — use `sleep infinity` directly:
```batch
start "WSL-KeepAlive-Ubuntu24" /min wsl -d Ubuntu-24.04 -u root sleep infinity
```
The title `"WSL-KeepAlive-Ubuntu24"` with quotes is the window title (standard `start` syntax), NOT the program name. Windows error "cannot find WSL-KeepAlive-Ubuntu24" means quotes were dropped or the command was run outside cmd.exe.

**Note:** Once a system service keeps a process alive in the distro, WSL2 may keep the distro running without the keepalive. The keepalive is a belt-and-suspenders safety net for when OpenClaw is restarting.

---

## Issue 3: Bot connects to Telegram but never replies

**Symptom:** Journal confirms `[telegram] [default] starting provider (@YourSentinelBot)`, user messages go unanswered.

**Root Cause A — Wrong LM Studio URL:**
OpenClaw (in WSL2) was configured to reach LM Studio at `http://192.168.50.74:1234/v1`. LM Studio binds only to `127.0.0.1:1234`. With `networkingMode=mirrored`, `localhost` in WSL2 correctly resolves to Windows localhost; the LAN IP does not.

**Fix:** Edit `/home/azfar/.openclaw/openclaw.json` (WSL path):
```json
"lmstudio": {
    "baseUrl": "http://localhost:1234/v1",
```
Then sync to last-good so the config watchdog doesn't revert it:
```bash
cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.last-good
```

**Root Cause B — No model loaded in LM Studio:**
LM Studio was running but had no model active. The API returns an empty `data` array. OpenClaw silently fails inference.

**Fix:** Open LM Studio → load `google/gemma-4-e4b` (or whichever model is configured in `openclaw.json` under `agents.defaults.model.primary`).

**Verify from WSL:**
```bash
curl -s -H "Authorization: Bearer <LMSTUDIO_APIKEY>" \
  http://localhost:1234/v1/models | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print([m['id'] for m in d.get('data',[])])"
```

---

## Issue 4: Config watchdog reverts openclaw.json

**Symptom:** Changes to openclaw.json disappear after OpenClaw restarts.

**Root Cause:** OpenClaw watches `openclaw.json` against `openclaw.json.last-good`. If sizes or hashes differ significantly, it reverts to last-good. If `config-health.json` has a stale baseline, even a valid config can appear suspicious.

**Fix:** Always update both files together after any edit:
```bash
cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.last-good
```

**Verify watchdog is safe:**
```bash
# SHA256 of current file must match lastKnownGood.hash in config-health.json
sha256sum /home/azfar/.openclaw/openclaw.json
cat /home/azfar/.openclaw/logs/config-health.json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); \
   print(list(d['entries'].values())[0]['lastKnownGood']['hash'])"
```

---

## Issue 5: Multiple instances of OpenClaw / batch scripts

**Symptom:** OpenClaw cycles in a 14-second loop. `journalctl` shows PIDs from multiple `systemd[NNN]` instances.

**Root Cause:** Multiple START_AI_STACK.bat windows running simultaneously. Each window's WSL polling loop (earlier `wsl -u azfar -- bash -c "systemctl --user is-active..."` loop) creates competing user sessions.

**Fix:** The START script now has a singleton lockfile guard at `%TEMP%\ai_stack_start.lock`. If you see the warning, kill stale cmd.exe instances:
```powershell
Get-Process cmd | ForEach-Object {
    $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
    if ($cl -match "START_AI_STACK") { Write-Host "PID $($_.Id): $cl" }
}
```
Delete stale lockfile manually if needed: `del %TEMP%\ai_stack_start.lock`

---

## Issue 6: Slow first response from bot

**Symptom:** Bot receives message, session stays in `state=processing` for 5–10 minutes. Journal shows `[diagnostic] stuck session`.

**Root Cause:** On first message after startup, OpenClaw loads all 200+ MetaMCP tools into the agent context, then sends a large prompt to LM Studio. Gemma 4E4B cold-start inference can take 5–10 minutes. Compaction of the large context may also timeout once.

**This is normal** on first message. Subsequent messages are faster once the model is warm and context is established. If it never resolves, restart OpenClaw to clear the stuck session:
```bash
wsl -d Ubuntu-24.04 -u root systemctl restart openclaw-gateway.service
```

---

## Issue 7: `wsl -u azfar` vs `wsl -u root` in scripts

**Root Cause:** Running diagnostic commands as `-u azfar` creates a new PAM login session for user azfar. This triggers `user@1000.service` lifecycle events in WSL2 systemd. When the session ends, it can stop/restart user services and potentially the distro.

**Rule:** All `wsl -d Ubuntu-24.04` management commands in batch scripts use `-u root`. This avoids creating user sessions for azfar.

---

## Connectivity Test — Full Stack

```bash
# Run from Windows PowerShell or CMD

# MetaMCP
curl -s -H "Authorization: Bearer <METAMCP_TOKEN>" -H "Accept: application/json, text/event-stream" ^
  -X POST http://127.0.0.1:12008/metamcp/default/mcp ^
  -H "Content-Type: application/json" ^
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1\"}}}"

# Google WS MCP
curl -s http://127.0.0.1:8089/health

# yt-dlp MCP
curl -s http://127.0.0.1:8088/health

# OpenClaw
curl -s -H "Authorization: Bearer <OPENCLAW_TOKEN>" http://127.0.0.1:18789/health

# LM Studio models (from WSL)
wsl -d Ubuntu-24.04 -u root curl -s ^
  -H "Authorization: Bearer <LMSTUDIO_APIKEY>" ^
  http://localhost:1234/v1/models
```
