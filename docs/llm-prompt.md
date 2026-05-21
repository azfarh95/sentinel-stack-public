# Sentinel Stack — LLM Installation Guide

---

## Instructions for the LLM

You are guiding a user through installing the Sentinel AI Stack on a fresh Windows 11 machine. Follow the phases in order. Do not skip ahead.

**Rules:**
- Ask one thing at a time. Do not dump a wall of commands.
- After every install step, verify it succeeded before continuing.
- If a step fails, diagnose and fix it before moving on — do not skip.
- Secrets the user provides go straight into Windows Credential Manager. Never print them back.
- Auto-generate anything that doesn't need to come from an external service (passwords, random keys).
- Use `$env:USERNAME` for the Windows username and `$wslUser` for the WSL username — never hardcode names.
- Tell the user to run commands by typing `!<command>` in Claude Code, or pasting into PowerShell/WSL.
- When a reboot is required, tell the user clearly, wait for them to confirm they're back, then continue.

---

## Phase 0 — Scope briefing

Introduce the stack to the user before doing anything. Say something like:

> "Sentinel is a personal AI assistant running on your local Windows machine, controlled via Telegram. It uses Claude (via OpenClaw) as the brain, connects to tools like Google Calendar, Maps, GitHub, and OneDrive via MCP servers, and exposes a secure Mini App dashboard inside Telegram. Everything runs locally — no cloud AI costs for the agent itself.
>
> Before we start installing, I need to know which tier you want, then collect your secrets, and then we'll install everything in the right order."

Then ask the user to choose a tier:

---

## Phase 1 — Feature tier selection

> **Tip for first-time users:** start with **Basic** to validate everything works end-to-end (~30 min). Comprehensive and Full add cloud integrations that each require external API setup (Google Cloud Console, Microsoft Azure, etc.) — each one adds 15-20 minutes. You can always add tiers later by collecting the missing secrets and editing `.env.local.template` to enable additional MCPs. **Don't try to do Full on the first install** unless you're already comfortable with Google Cloud + Azure + Cloudflare.

Ask the user:

> "Which installation tier do you want?
>
> **Basic** — Core AI assistant + dashboard
> - Telegram AI bot (OpenClaw + Claude/local model)
> - Memory (long-term recall across sessions)
> - Reminders (scheduled Telegram messages)
> - Sentinel Mini App dashboard (2FA-gated)
> - Watchdog bot (monitoring + alerts)
>
> **Comprehensive** — Basic + productivity integrations
> - Everything in Basic
> - Google Workspace (Gmail, Calendar, Drive)
> - Google Maps (directions + place search)
> - GitHub (repos, issues, PRs, code search)
> - Language translation (offline, local)
>
> **Full** — Comprehensive + media + public access + V3 features
> - Everything in Comprehensive
> - OneDrive + PDF parsing (Azure Document Intelligence)
> - Video/photo downloads (YouTube, Instagram, TikTok)
> - Public URL via Cloudflare Tunnel (access dashboard anywhere)
> - Inference Bridge with 3-way model routing (simple/complex/coding)
> - Live Browser panel — see the agent's Playwright session in the mini app (V3)
> - Per-tester daily caps for shared OpenRouter beta (V5 Phase A scaffolding)
> - OpenRouter integration (cloud models alongside local)"

Save the chosen tier. It determines which secrets to collect and which Docker services to start.

---

## Phase 2 — Secret collection

Collect secrets upfront and save each one to Windows Credential Manager immediately after the user provides it. Never store in a file.

Tell the user:
> "I'll collect everything I need now and store it securely in Windows Credential Manager. You won't need to enter these again. Let's go one at a time."

### Step-by-step secret prompts

For **all tiers**, collect in this order:

**1. Windows username**
```powershell
$env:USERNAME
```
Run this — the output is the Windows username. Store it as `$winUser` for use in paths.

**2. Sentinel bot token**
> "Create a Telegram bot via @BotFather → /newbot. Paste the token here."

Save: `cmdkey /generic:sentinel-miniapp /user:telegram_bot_token /pass:<token>`

**3. Watchdog bot token**
> "Create a second bot via @BotFather → /newbot. This one is the management/watchdog bot. Paste the token."

Save: `cmdkey /generic:sentinel-watchdog /user:bot_token /pass:<token>`

**4. Your Telegram user ID**
> "Message @userinfobot on Telegram. It will reply with your numeric user ID. Paste it here."

Store for use in config files.

**5. TOTP secret** (auto-generate)
```powershell
py -c "import pyotp; print(pyotp.random_base32())"
```
Save output: `cmdkey /generic:sentinel-miniapp /user:totp_secret /pass:<output>`
Tell the user: "I've generated a TOTP secret. You'll scan a QR code at the end to pair it with Google Authenticator."

**6. Mini App secret** (auto-generate)
```powershell
py -c "import secrets; print(secrets.token_hex(32))"
```
Save: `cmdkey /generic:sentinel-miniapp /user:mini_app_secret /pass:<output>`

**7. PostgreSQL password** (auto-generate)
```powershell
py -c "import secrets; print(secrets.token_hex(16))"
```
Store as `$pgPassword` — used when writing `.env.local`.

**8. Better Auth secret** (auto-generate)
```powershell
py -c "import secrets; print(secrets.token_hex(32))"
```
Save: `cmdkey /generic:sentinel-miniapp /user:better_auth_secret /pass:<output>`

**9. LM Studio API key** — get from LM Studio after install (Phase 3e). Don't collect now; do it inline when LM Studio is running. Save as `cmdkey /generic:sentinel-watchdog /user:lm_api_key /pass:<key>`.

**10. MetaMCP bearer token** — auto-generated by MetaMCP on first start. Read from container after Phase 5c, save to WCM as `cmdkey /generic:sentinel-miniapp /user:metamcp_bearer_token /pass:<token>`. Used by mini app to call the V3 browser panel and any other MetaMCP-routed traffic.

---

### Note on `.env.local` (do NOT hand-write)

V2.14+ canonicalises secrets in Windows Credential Manager. The `.env.local` file is **generated** by `scripts\sync_env_from_wcm.ps1` from `.env.local.template` (which is committed). Don't write `.env.local` directly — set the WCM entries below, then run the sync script.

The template uses `__WCM_<key>__` placeholders that get substituted at boot. `START_AI_STACK.bat` runs the sync as step `[0/7]` automatically.

---

For **Comprehensive** and **Full**, also collect:

**9. GitHub PAT**
> "Go to github.com → Settings → Developer settings → Personal access tokens → Generate new token (classic). Scopes needed: repo, workflow. Paste it here."

Save: `cmdkey /generic:sentinel-miniapp /user:github_pat /pass:<token>`
Also push to GitHub Actions: `gh secret set PAT --body <token> --repo azfarh95/sentinel-stack-public`

**10. Google Maps API key**
> "Go to console.cloud.google.com → APIs & Services → Credentials → Create API key. Enable the Maps JavaScript API and Directions API. Paste the key here."

Save: add to `.env.local` as `GOOGLE_MAPS_API_KEY=<key>`

**11. Google OAuth credentials**
> "In Google Cloud Console → APIs & Services → OAuth 2.0 Client IDs → Create. Application type: Web. Download the JSON. Paste the client_id and client_secret here."

Save to `.env.local`.

---

For **Full**, also collect:

**12. Cloudflare Tunnel** — deferred to Phase 5 (requires browser login).

**13. Azure Document Intelligence** (OneDrive PDF parsing)
> "Go to portal.azure.com → Create a Document Intelligence resource → copy the endpoint and key."

Save: `cmdkey /generic:sentinel-miniapp /user:docintel_key /pass:<key>`

**14. OneDrive client secret** (Microsoft Graph OAuth)
> "Go to portal.azure.com → App registrations → New registration. Set redirect URI for OneDrive MCP. Copy the client_secret."

Save: `cmdkey /generic:sentinel-miniapp /user:onedrive_client_secret /pass:<secret>`

**15. SMDL bot token** (separate Telegram bot for the standalone media downloader)
> "Create a third bot via @BotFather → /newbot. This is the SMDL bot. Paste the token."

Save: `cmdkey /generic:sentinel-miniapp /user:smdl_bot_token /pass:<token>`

**16. OpenRouter API key** (optional — for cloud-model fallback or guest tier)
> "Go to openrouter.ai → Keys → Create Key. Paste it here."

Save: `cmdkey /generic:sentinel-miniapp /user:openrouter_api_key /pass:<key>`

This is optional but recommended — the mini app can switch the active model to OpenRouter free tier with one tap (Settings → Model → + OpenRouter), useful for beta testing or guest sessions.

---

## Phase 3 — Pre-reboot installation

Install everything that doesn't require WSL2 first. WSL2 installation is Phase 4 (it needs a reboot).

### 3a. Prerequisites check
```powershell
# Check Python
py --version

# Check Git
git --version

# Check winget
winget --version
```
If any are missing, install them before continuing.

### 3b. Clone the repo
```powershell
git clone https://github.com/azfarh95/sentinel-stack-public-public.git metamcp-local
cd metamcp-local
```

### 3c. Python packages
```powershell
py -m pip install flask pyotp qrcode keyring
```

### 3d. Docker Desktop
```powershell
winget install Docker.DockerDesktop
```
After install, launch Docker Desktop. Do not configure WSL integration yet — that's after the reboot.

**Verify:**
```powershell
docker --version
```

### 3e. LM Studio
```powershell
winget install ElementLabs.LMStudio
```
After install, open LM Studio:
1. Settings → Hardware → Backend: `Vulkan` (more stable than ROCm on Windows for AMD GPUs; CUDA auto-selected on NVIDIA)
2. Search: **`Qwen3-30B-A3B-GGUF`** (Qwen 3.6 27B for 24+ GB VRAM) or `Qwen3-6B-GGUF` (for lower VRAM)
3. Download `Q4_K_M` quantization
4. Optional but recommended for V3: also download **`Qwen2.5-Coder-32B-Instruct-GGUF`** Q4_K_M — the inference bridge's 3-way router will use this for coding prompts (code blocks / dev keywords)
5. Developer → Settings → API Keys → Create → copy the key (you'll save to WCM in Phase 5b)
6. Load the model and start the server on port `1234`

Tell the user: "LM Studio needs to be set up manually via its UI. Let me know when the model is loaded, the API key is created, and the local server is running on port 1234."

**Verify (with auth):**
```powershell
curl -H "Authorization: Bearer <LMSTUDIO_API_KEY>" http://localhost:1234/v1/models
```

### 3f. Write config files

Use collected secrets to write config files. Replace all `azfar` references with `$env:USERNAME`.

**`config.json`:**
```powershell
$winUser   = $env:USERNAME
$botToken  = (cmdkey /list:sentinel-miniapp | ...)   # retrieve from WCM
# Write config.json using stored values
```

Write `config.json`:
```json
{
  "telegram_bot_token": "<SENTINEL_BOT_TOKEN>",
  "telegram_chat_ids": {
    "dm": "<OWNER_TELEGRAM_ID>",
    "group": "<OWNER_TELEGRAM_ID>"
  },
  "mini_app_secret": "<MINI_APP_SECRET>",
  "totp_secret": "<TOTP_SECRET>",
  "mini_app_url": "https://your-domain.example.com"
}
```

**`.env.local`:**
```
POSTGRES_PASSWORD=<pgPassword>
BETTER_AUTH_SECRET=<authSecret>
GITHUB_PAT=<github_pat>
```
Add tier-specific keys (Google Maps, Azure) if applicable.

**`watchdog/config.json`:**
```json
{
  "owner_chat_id": <OWNER_TELEGRAM_ID>,
  "wsl_distro": "Ubuntu-24.04",
  "openclaw_service": "openclaw-gateway.service",
  "compose_dir": "C:\\Users\\<winUser>\\metamcp-local",
  "compose_files": ["docker-compose.local.yml"],
  "alert_interval_seconds": 1800,
  "auto_restart": true,
  "digest_enabled": true,
  "digest_time": "08:00",
  "lm_studio_exe": null,
  "infer_bridge": null,
  "sentinel_bridge": null,
  "github_repo": "azfarh95/sentinel-stack-public",
  "github_sync_interval": 300
}
```

---

## Phase 4 — WSL2 install + reboot

Tell the user:
> "We're about to install WSL2. This requires a reboot. I'll pick up exactly where we left off after you restart."

```powershell
wsl --install
```

**Tell the user to reboot now. Wait for them to confirm they're back.**

After reboot:
```powershell
# Install Ubuntu
wsl --install -d Ubuntu-24.04
wsl --set-default Ubuntu-24.04

# Configure Docker Desktop WSL integration
# Docker Desktop → Settings → Resources → WSL Integration → enable Ubuntu-24.04
```

Tell the user to confirm Docker Desktop is showing Ubuntu-24.04 enabled before continuing.

**Verify:**
```powershell
wsl -l -v
# Ubuntu-24.04 should show Version 2, State: Running
docker --version  # confirm Docker still works post-reboot
```

---

## Phase 5 — Post-reboot installation

### 5a. WSL2 Ubuntu setup
```bash
# Run inside WSL2
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git build-essential

# Node 20 via nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc
nvm install 20 && nvm use 20
```

Store the WSL username:
```bash
whoami   # e.g. azfar — use this as $wslUser
```

### 5b. OpenClaw
```bash
npm install -g --prefix ~/.npm-global openclaw
echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
openclaw --version
```

**Initialise OpenClaw config files.** Run as `<wslUser>` (NOT root) so files end up in the right home dir:

```bash
mkdir -p ~/.openclaw/credentials
mkdir -p ~/.openclaw/agents/main/agent
```

**`~/.openclaw/openclaw.json`** (top-level config — agent talks to MetaMCP via this):
```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "botToken": "<SENTINEL_BOT_TOKEN>",
      "groups": {
        "*": { "requireMention": true }
      }
    }
  },
  "mcp": {
    "servers": {
      "metamcp": {
        "url": "http://localhost:12008/metamcp/default/mcp",
        "transport": "streamable-http",
        "headers": {
          "Authorization": "Bearer <METAMCP_BEARER_TOKEN>"
        }
      }
    }
  },
  "messages": { "groupChat": { "visibleReplies": "automatic" } },
  "commands": { "ownerAllowFrom": ["telegram:<OWNER_TELEGRAM_ID>"] },
  "agents": {
    "defaults": {
      "workspace": "/home/<wslUser>/.openclaw/workspace",
      "model": { "primary": "lmstudio/qwen/qwen3.6-27b", "fallbacks": [] }
    }
  }
}
```

**`~/.openclaw/credentials/telegram-default-allowFrom.json`** (whitelist of chat_ids that the bot will respond to):
```json
{ "version": 1, "allowFrom": ["<OWNER_TELEGRAM_ID>"] }
```

**`~/.openclaw/agents/main/agent/auth-profiles.json`** (where API keys live for the agent's model providers):
```json
{
  "version": 1,
  "profiles": {
    "lmstudio:default": {
      "type": "api_key",
      "provider": "lmstudio",
      "key": "<LMSTUDIO_API_KEY>"
    }
  }
}
```

Get the `<LMSTUDIO_API_KEY>` from LM Studio → Developer → Settings → API Keys → Create. Paste it back here and also save to WCM:
```powershell
cmdkey /generic:sentinel-watchdog /user:lm_api_key /pass:<LMSTUDIO_API_KEY>
```

The MetaMCP bearer token: read from `.env.local` after stack starts (auto-generated by MetaMCP) and save to WCM:
```powershell
# Run after MetaMCP container is up (Phase 5c)
$tok = (docker exec metamcp printenv METAMCP_API_KEY)
cmdkey /generic:sentinel-miniapp /user:metamcp_bearer_token /pass:$tok
# Also paste into ~/.openclaw/openclaw.json above
```

**Systemd service** — replace `<wslUser>` with the output of `whoami`:
```bash
sudo tee /etc/systemd/system/openclaw-gateway.service > /dev/null <<EOF
[Unit]
Description=OpenClaw AI Gateway
After=network.target

[Service]
Type=simple
User=<wslUser>
WorkingDirectory=/home/<wslUser>/.openclaw
ExecStart=/home/<wslUser>/.npm-global/bin/openclaw serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable openclaw-gateway.service
sudo systemctl start openclaw-gateway.service
sudo systemctl status openclaw-gateway.service
```

Expected: `active (running)`

### 5c. Docker stack
```powershell
# From repo root (Windows PowerShell)
docker compose -f docker-compose.local.yml up -d
```

Wait 30 seconds, then verify all containers are `Up`:
```powershell
docker ps --format "table {{.Names}}\t{{.Status}}"
```

Expected containers (Basic): `metamcp`, `postgres`, `memory-mcp`, `reminders-mcp`
Comprehensive adds: `google-workspace-mcp`, `maps-mcp`, `github-mcp`, `translate-mcp`, `libretranslate`
Full adds: `onedrive-mcp`, `ytdlp-mcp`

**Verify MetaMCP UI:** open `http://localhost:12008`

### 5d. Watchdog bot
```powershell
# Test first
py watchdog\watchdog.py
```
If it starts and sends a Telegram message — good. Ctrl+C to stop, then register:
```powershell
scripts\REGISTER_BACKUP_TASKS.ps1
```

### 5e. Mini App bridge
```powershell
py sentinel-miniapp-v2\bridge.py
```
Should print: `Sentinel Mini App v2 Bridge on :8098`

### 5f. Cloudflare Tunnel (Full tier only)
```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel login        # opens browser — user must authorise
cloudflared tunnel create sentinel
```

Write `~/.cloudflared/config.yml` — replace `<winUser>` and `<tunnel-id>`:
```yaml
tunnel: <tunnel-id>
credentials-file: C:\Users\<winUser>\.cloudflared\<tunnel-id>.json
ingress:
  - hostname: your-domain.example.com
    service: http://127.0.0.1:8098
  - service: http_status:404
```

```powershell
cloudflared tunnel route dns sentinel your-domain.example.com
cloudflared tunnel run sentinel
```

### 5g. Playwright MCP (browser automation — required for V3 Browser panel)

Playwright MCP runs as a Windows-side Node process spawned by Task Scheduler.

```powershell
# Install Node.js if not present
winget install OpenJS.NodeJS.LTS

# Verify
node --version
npx --version
```

Register the Playwright watcher task (auto-restarts if it crashes):
```powershell
schtasks /Create /TN "Playwright MCP Watcher" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\<winUser>\metamcp-local\scripts\playwright-mcp-watcher.ps1" /SC ONSTART /RU SYSTEM /F
```

The watcher runs `playwright-mcp-server.bat` which launches:
```
npx @playwright/mcp@latest --port 8931 --browser chrome --allowed-hosts * --shared-browser-context
```

**The `--shared-browser-context` flag is critical** — without it, the agent and the mini app's Browser panel will fight over the single Chromium instance and you'll see "browser is currently in use by another session" errors.

**Verify:**
```powershell
netstat -ano | findstr ":8931 :8932" | findstr LISTENING
# Should show both ports up (8931 = Playwright, 8932 = IPv4 proxy)
```

### 5h. Register all Task Scheduler tasks
```powershell
scripts\REGISTER_BACKUP_TASKS.ps1
```

---

## Phase 6 — TOTP pairing

Generate the QR-code page from the WCM-stored `totp_secret`:
```powershell
py -c @"
import keyring, pyotp, qrcode, base64
secret = keyring.get_password('sentinel-miniapp', 'totp_secret')
uri = pyotp.TOTP(secret).provisioning_uri(name='Sentinel Mini App', issuer_name='Sentinel')
img = qrcode.make(uri)
img.save('sentinel-miniapp-v2/totp_setup.png')
print('Saved sentinel-miniapp-v2/totp_setup.png — open and scan')
"@
```
Open `sentinel-miniapp-v2\totp_setup.png` and scan with Google Authenticator. Test a code in the mini app login before closing.

After confirming the TOTP works:
```powershell
del sentinel-miniapp-v2\totp_setup.png
```

---

## Phase 7 — Full verification

Run every check. Do not declare success until all pass.

```powershell
# All ports
@(12008,8092,8087,8089,8090,8091,8093,8094,8088,8095,8098,8099,1234) | ForEach-Object {
    $r = Test-NetConnection -ComputerName 127.0.0.1 -Port $_ -WarningAction SilentlyContinue
    "$_ : $(if($r.TcpTestSucceeded){'UP'}else{'DOWN'})"
}

# OpenClaw
wsl -d Ubuntu-24.04 -u root -- systemctl is-active openclaw-gateway.service

# Mini App reachable
curl http://localhost:8098/api/auth/status
```

Tell the user to:
1. Send `/status` to the Watchdog bot — should reply with service statuses
2. Send `/dashboard` to the Sentinel bot — should send a Mini App button
3. Tap the Mini App button → login with TOTP → confirm the dashboard loads

**V3 Browser panel verification:**
4. In the mini app, tap **🌐 Browser** on home nav. Status should say "Connecting…" then "X frames received".
5. Send the AI bot: *"navigate to example.com"*. Within ~2 seconds the panel should render the page. If you see "browser is currently in use by another session", check that Playwright MCP is launched with `--shared-browser-context`.

**Pre-commit hook (recommended for contributors):**
```powershell
@'
#!/bin/sh
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(git rev-parse --show-toplevel)/scripts/check_secrets.ps1"
'@ | Set-Content -Path .git\hooks\pre-commit -NoNewline -Encoding UTF8
```
Tests on commit that no plaintext secrets (sk-, ghp_, AIza, telegram tokens etc.) sneak in.

**WCM sync verification:**
```powershell
# Should regenerate .env.local from WCM with no errors
scripts\sync_env_from_wcm.ps1
# Output: [sync] C:\Users\<winUser>\metamcp-local\.env.local regenerated from WCM (6 secrets)
```

---

## Phase 8 — Start script for future use

From now on, start the full stack with:
```powershell
scripts\START_AI_STACK.bat
```

Stop:
```powershell
scripts\STOP_AI_STACK.bat
```

---

## Port reference

| Port | Service |
|------|---------|
| 1234 | LM Studio |
| 5050 | LibreTranslate |
| 8087 | Reminders MCP |
| 8088 | yt-dlp MCP (Full) |
| 8089 | Google Workspace MCP (Comprehensive+) |
| 8090 | Maps MCP (Comprehensive+) |
| 8091 | GitHub MCP (Comprehensive+) |
| 8092 | Memory MCP |
| 8093 | OneDrive MCP (Full) |
| 8094 | Translate MCP (Comprehensive+) |
| 8095 | Inference Bridge (Full) |
| 8098 | Mini App Bridge |
| 8099 | Watchdog HTTP |
| 9433 | PostgreSQL |
| 12008 | MetaMCP |
| 18789 | OpenClaw gateway |

---

## Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| OpenClaw not responding | systemd crashed | `wsl -d Ubuntu-24.04 -u root -- systemctl restart openclaw-gateway.service` |
| MetaMCP shows no tools | containers still starting | Wait 60s → `docker restart metamcp` |
| Mini App "Failed to load config" | bridge crashed | Check for Python errors, restart `py bridge.py` |
| LM Studio unreachable | model not loaded | Open LM Studio → load model → start server |
| TOTP always fails | clock drift | `w32tm /resync` in PowerShell (admin) |
| Docker containers restart loop | `.env.local` wrong | Check values, `docker compose down && docker compose up -d` |
| WSL2 not starting after reboot | virtualisation disabled | Enable in BIOS: AMD-V / Intel VT-x |
| Browser panel shows "browser in use by another session" | Playwright MCP missing `--shared-browser-context` flag | Edit `scripts\playwright-mcp-server.bat` and `playwright-mcp-watcher.ps1` to add the flag, kill node processes, restart |
| Bot ignores all `/start` from non-owner users | OpenClaw `allowFrom` whitelist doesn't include their chat_id | Edit `~/.openclaw/credentials/telegram-default-allowFrom.json`, add their chat_id, restart `openclaw-gateway.service` |
| LM Studio `/v1/models` returns 401 in mini app | API key not in WCM | `cmdkey /generic:sentinel-watchdog /user:lm_api_key /pass:<key>` then restart inference bridge |
| Mini app "Inference: Idle" but model loaded | bridge cant auth to LM Studio | Same fix as above — check WCM has `lm_api_key` |
| Two OpenClaw processes fighting (409 conflict on Telegram getUpdates) | duplicate systemd units (user-level + system-level) | `systemctl --user disable openclaw-gateway && systemctl --user stop openclaw-gateway` (run as `<wslUser>`) |
| `.env.local` keeps reverting to defaults | running `docker compose` directly without `START_AI_STACK.bat` | Use the bat file (it runs the WCM sync first) or run `scripts\sync_env_from_wcm.ps1` manually before compose |
| Pre-commit hook fails on `sk-or-` / `ghp_` patterns | a real secret made it into a tracked file | Move the secret to WCM, replace with `__WCM_<key>__` placeholder in template |

---

## Backup checklist (before new machine)

**WCM secrets** (export via `scripts\export-sentinel.ps1`):
- [ ] All 16 cmdkey targets under `sentinel-miniapp` and `sentinel-watchdog`
- [ ] TOTP base32 secret backed up to a separate password manager (NOT just WCM)

**Config files** (committed to git but with sanitized values):
- [ ] `.env.local.template` — placeholders for the 6 WCM-resolved secrets
- [ ] `config.json` (private — has chat IDs)
- [ ] `watchdog/config.json` (private — has owner chat ID)
- [ ] `watchdog/contacts.json` (private — registered guests)

**OpenClaw config** (at `~/.openclaw/` in WSL2):
- [ ] `openclaw.json` (top-level config)
- [ ] `agents/main/agent/auth-profiles.json` (LM Studio + OpenRouter API keys)
- [ ] `credentials/telegram-default-allowFrom.json` (whitelisted chat IDs)
- [ ] `credentials/telegram-pairing.json` (pending guest registrations)

**Data**:
- [ ] LM Studio model files (or plan to re-download — Qwen3.6-27B + Qwen2.5-Coder-32B)
- [ ] Playwright Chromium cache (`~\AppData\Local\ms-playwright\`)
- [ ] reminders-mcp `/data/reminders.db` and `/data/scheduler.db` (Docker volumes)
- [ ] memory-mcp data volume
- [ ] watchdog `guest_usage.db` (per-tester caps state)

**Full system**:
- [ ] WSL2 export: `wsl --export Ubuntu-24.04 ubuntu-backup.tar`
- [ ] Run: `scripts\export-sentinel.ps1` — produces full archive + RESTORE.md
- [ ] Cloudflare tunnel credentials at `~\.cloudflared\<tunnel-id>.json`
