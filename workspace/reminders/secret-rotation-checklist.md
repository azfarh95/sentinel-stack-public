# 🔐 Secret Rotation Checklist — before sharing private repo

Goal: every value that has ever been committed to `azfarh95/sentinel-stack-public` git history becomes **dead and useless** to anyone who reads the repo, before you invite an outside collaborator.

Estimated total time: ~30-45 min, all free, all reversible if you mess one up.

## ⚡ TL;DR — paste-and-run CLI commands

Built `scripts/rotate.ps1` to make 11 of the 14 secrets one-line operations. After you generate the new value in the relevant browser tab:

```powershell
# AI bot token (BotFather /revoke → copy new token)
.\scripts\rotate.ps1 telegram-ai           <new-token>

# Watchdog/middleware bot token
.\scripts\rotate.ps1 telegram-watchdog     <new-token>

# Testbot token (ClaudeAssistant)
.\scripts\rotate.ps1 telegram-testbot      <new-token>

# Tavily API key
.\scripts\rotate.ps1 tavily                <new-key>

# Azure Speech key
.\scripts\rotate.ps1 azure-speech          <new-key>

# MetaMCP bearer token
.\scripts\rotate.ps1 metamcp               <new-token>

# LM Studio API key
.\scripts\rotate.ps1 lmstudio              <new-key>

# OpenClaw gateway auth (auto-generates if you omit value)
.\scripts\rotate.ps1 gateway-auth

# GitHub PAT
.\scripts\rotate.ps1 github-pat            <new-token>

# TOTP secret (regenerates, you re-scan QR after)
.\scripts\rotate.ps1 totp

# Cloudflare Tunnel (prints the cloudflared commands; not auto-run)
.\scripts\rotate.ps1 cloudflare-tunnel
```

Each command does: WCM update + openclaw.json edit + service restart + reports done. Never echoes the value back.

The remaining 3 are interactive (need browser/phone):
- `.\scripts\rotate.ps1 telethon`         — prints Python REPL instructions for SMS auth
- `.\scripts\rotate.ps1 google-oauth`     — prints browser-flow instructions
- `.\scripts\rotate.ps1 microsoft-oauth`  — prints browser-flow instructions

After running the Telethon Python flow, finalize with:
```powershell
.\scripts\rotate.ps1 telethon-session-string <the-printed-session-string>
```

---

## Detailed steps (the verbose version, for reference if the script fails)

---

## Why this is necessary

A `git log -p` search of the private repo finds **14 commits** containing Telegram-bot-token-shaped strings, plus more for API keys and OAuth tokens. Even if I scrubbed history, anyone with an existing clone (or GitHub's commit cache) could still read them. **The only true safety is rotation: make the old values stop working.**

---

## What you need before starting

- A browser logged into the relevant accounts (Telegram, Tavily, Azure, OpenRouter, Google, Microsoft, Cloudflare, GitHub, LM Studio, MetaMCP)
- The keyring CLI working (it is — Phase A backup script verified yesterday)
- ~30 min uninterrupted

**Do NOT paste any new key value into Claude Code chat.** I'll write you scripts that pull from WCM, but never see the values themselves. If a smoke test needs a secret, the script reads it from WCM at runtime — never from chat.

---

## The list (in order — easiest first)

### 1. Telegram bot tokens (3 bots)

For each bot:
- Open Telegram → DM `@BotFather`
- Send `/revoke` → pick the bot → confirm
- BotFather replies with the new token
- Copy it into WCM (don't paste in chat)

| Bot username | WCM key | Touched files after rotation |
|---|---|---|
| `@YourSentinelBot` | `telegram_bot_token` | OpenClaw `~/.openclaw/openclaw.json:.channels.telegram.botToken`, restart with `sudo systemctl restart openclaw-gateway` |
| `@YourWatchdogBot` | (watchdog uses it via `keyring`) | restart watchdog: `taskkill` the python process + relaunch from scheduled task |
| `@SentinelClaudeAssistantBot` (testbot) | `TESTBOT_TOKEN` in `~/.claude/projects/.../V4/ClaudeAssistant/.env.testenv` | edit the env file, no restart needed |

After all 3 rotated: send any message to each bot in Telegram to confirm they reply.

### 2. Tavily API key

- https://app.tavily.com/dashboard — log in
- API Keys → revoke current → generate new
- Copy new key
- Paste into `~/.openclaw/openclaw.json:.plugins.entries.tavily.config.webSearch.apiKey`
- `sudo systemctl restart openclaw-gateway`
- Smoke test: ask Sentinel "search the web for X" — should return Tavily results

### 3. Azure Speech key

- https://portal.azure.com → resource group → Speech resource → Keys & Endpoint
- Click **Regenerate Key 1** (the active one)
- Copy the new value
- Update `~/.openclaw/openclaw.json:.talk.providers.azure-speech.apiKey`
- `sudo systemctl restart openclaw-gateway`
- Smoke test: trigger a TTS reply (Sentinel agent → /speak X)

### 4. MetaMCP bearer token

- http://localhost:12008 → log in → Settings → API Tokens
- Revoke `sk_mt_LiNBl...` (current)
- Generate new
- Update WCM key `metamcp_bearer_token` (replace value)
- Update `~/.openclaw/openclaw.json:.mcp.servers.metamcp.headers.Authorization` (this is `Bearer <token>`, replace just the token part)
- Restart bridge.py + OpenClaw
- Smoke test: ask Sentinel to use any MCP tool ("what's the weather?")

### 5. LM Studio API key

- LM Studio app → Developer tab → Server Settings → API Key
- Generate new
- Update `~/.openclaw/openclaw.json:.models.providers.lmstudio.apiKey`
- Restart OpenClaw
- Smoke test: any chat with Sentinel will fail loudly if this is wrong

### 6. OpenClaw gateway auth token

- This is the OpenClaw web-UI auth token (`.gateway.auth.token` in `openclaw.json`)
- Generate fresh: `python -c "import secrets; print(secrets.token_hex(24))"` in PowerShell
- Replace value in `~/.openclaw/openclaw.json:.gateway.auth.token`
- Restart OpenClaw
- Smoke test: web UI at http://127.0.0.1:18789 should prompt for the new token; old value should fail

### 7. Telethon session string (chat composer)

- This is harder — it's tied to your Telegram user account, not a bot
- In Python:
  ```python
  from telethon import TelegramClient
  from telethon.sessions import StringSession
  api_id = <from WCM telethon_api_id>
  api_hash = <from WCM telethon_api_hash>
  with TelegramClient(StringSession(), api_id, api_hash) as c:
      c.start()  # prompts for phone, then SMS code
      print(c.session.save())
  ```
- New session string → replace WCM value `telethon_session`
- Old session is auto-invalidated by Telegram when new one is created
- Restart bridge.py
- Smoke test: chat composer in mini-app (send "hi" via the lower box → should land in Sentinel chat)

**Note**: api_id and api_hash for Telethon do NOT need rotation — they identify the API client app, not your account. Treat them like config, not secrets.

### 8. GitHub PAT

- https://github.com/settings/tokens
- Find current token → Delete
- Generate new (classic) with same scopes (probably `repo` + `read:user` based on github-mcp container needs)
- Update WCM key `github_pat`
- Restart `github-mcp` container: `docker restart github-mcp`
- Smoke test: ask Sentinel to "list my recent GitHub issues"

### 9. Google OAuth refresh tokens (Drive / Gmail / Calendar)

- https://myaccount.google.com/permissions
- Find "Sentinel google-workspace-mcp" (or whatever the OAuth app is named) → Remove access
- Re-authenticate by hitting the auth URL the google-workspace-mcp container exposes (probably http://localhost:8089/auth)
- This generates fresh tokens, written to the container's persistent volume
- Smoke test: ask Sentinel to "summarize my recent emails"

### 10. Microsoft Graph OAuth (OneDrive)

- https://account.microsoft.com/privacy/app-access — revoke OneDrive MCP app
- Re-auth via http://localhost:8093/auth (per the OneDrive MCP setup)
- Smoke test: ask Sentinel to "read X file from OneDrive"

### 11. TOTP secret (mini-app 2FA)

- Note: this isn't an external service — it's local. Anyone with read access to `config.json` can compute valid 2FA codes
- Re-roll: delete the existing TOTP entry from `config.json` or WCM, restart bridge.py, scan the new QR (printed at `~/sentinel-miniapp-v2/totp_setup.html` on startup)
- Update Authenticator app with the new secret

### 12. Cloudflare Tunnel credentials

- Run: `cloudflared tunnel delete sentinel` (kills old)
- Run: `cloudflared tunnel create sentinel-2` (new tunnel UUID + credentials.json)
- Update `cloudflared` config to point at new tunnel UUID
- Update DNS routing: `cloudflared tunnel route dns sentinel-2 your-domain.example.com`
- Restart cloudflared service
- Smoke test: open https://your-domain.example.com on your phone — should still land on the mini-app

---

## After everything rotated

1. **Verify Sentinel agent end-to-end**: send a message that exercises every tool (search, OneDrive, GitHub, TTS, etc.). If anything fails, that's an unrotated dependency.
2. **Verify mini-app login** + chat composer + browser panel.
3. **Optional**: run `git filter-repo` to scrub the dead values from history. Cosmetic only at this point — old values can't do damage anymore. The actual command to do that:
   ```bash
   git filter-repo --replace-text <(echo "OLD_TELEGRAM_TOKEN==>***ROTATED***")
   ```
   But honestly, don't bother — the values are useless now.
4. **Send the read-only collaborator invite** to CT0388044:
   ```bash
   gh api -X DELETE /repos/azfarh95/sentinel-stack-public/invitations/<existing-invite-id>
   gh api -X PUT /repos/azfarh95/sentinel-stack-public/collaborators/CT0388044 -f permission=pull
   ```

---

## What NOT to do

- ❌ Don't paste any new value into Claude Code chat
- ❌ Don't update files via the bridge.py editor while the bridge is running and reading from those files (small race window — restart after each config edit)
- ❌ Don't skip the smoke test for any secret. If you don't verify it works, you find out at the worst time
- ❌ Don't `git push` any of the new values back into the repo. WCM only.

---

## Recovery paths

If you rotate something and the system breaks:
- Most secrets are stored in WCM via `keyring`. Restore the OLD value temporarily by re-pasting into WCM. The old value still works (you haven't told the provider it's the new authoritative one) until you go to the provider and confirm the rotation.
- For Telegram BotFather `/revoke`, the OLD token DOES stop working immediately. The only way back is to use the new value.
- For OAuth (Google, Microsoft), re-authenticating is always safe — generates fresh tokens. Worst case, you re-auth a second time.

If you get stuck on step N: stop, message me on a new Claude Code session with which step + what error. Don't keep going through the list with a broken intermediate state.
