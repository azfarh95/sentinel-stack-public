# scripts/rotate.ps1 — single-command secret rotation for Sentinel
#
# Usage:
#   .\scripts\rotate.ps1 <secret-name> <new-value>
#
# Or to see what's available:
#   .\scripts\rotate.ps1 list
#
# The script:
#   - Updates WCM (keyring) and/or openclaw.json field
#   - Restarts whichever service consumes that secret
#   - Best-effort smoke-test where possible
#   - Never prints the new value back to console (shoulder-surf safety)
#
# Pure-CLI secrets (this script handles): telegram-ai, telegram-watchdog,
# telegram-testbot, tavily, azure-speech, metamcp, lmstudio, gateway-auth,
# github-pat, totp, cloudflare-tunnel
#
# Interactive secrets (must be done by hand): telethon (SMS code),
# google-oauth (browser), microsoft-oauth (browser). The script prints
# instructions if you ask for those.

param(
    [Parameter(Mandatory=$true, Position=0)] [string] $Secret,
    [Parameter(Position=1)] [string] $NewValue
)

$ErrorActionPreference = "Stop"
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $Py) { Write-Error "No Python on PATH"; exit 1 }

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Set-WcmSecret {
    param([string]$Service, [string]$User, [string]$Value)
    & $Py -c "import keyring; keyring.set_password('$Service', '$User', '''$Value''')"
    if ($LASTEXITCODE -ne 0) { throw "keyring.set_password failed for $Service/$User" }
    Write-Host "  ✓ WCM updated: $Service/$User" -ForegroundColor Green
}

function Update-OpenclawJson {
    param([string]$JqPath, [string]$Value)
    # JqPath is dotted: .channels.telegram.botToken
    # Use jq in WSL to atomically edit ~/.openclaw/openclaw.json.
    # IMPORTANT: keep this as ONE bash line. Multi-line bash via `wsl -e bash -c`
    # fails because PowerShell here-strings carry CRLF line endings, which
    # break bash if/then/fi parsing. && / || chains avoid that pitfall.
    $cmd = "jq --arg v '$Value' '$JqPath = `$v' ~/.openclaw/openclaw.json > ~/.openclaw/openclaw.json.new && [ -s ~/.openclaw/openclaw.json.new ] && mv ~/.openclaw/openclaw.json.new ~/.openclaw/openclaw.json && echo OK || (rm -f ~/.openclaw/openclaw.json.new; echo FAIL)"
    $result = wsl -d Ubuntu-24.04 -e bash -c $cmd
    if ($result -notmatch "OK") { throw "openclaw.json update failed: $result" }
    Write-Host "  ✓ openclaw.json updated: $JqPath" -ForegroundColor Green
}

function Restart-OpenclawGateway {
    Write-Host "  → reloading OpenClaw (SIGUSR1)..." -ForegroundColor Yellow
    wsl -d Ubuntu-24.04 -u root -e bash -c "systemctl kill -s SIGUSR1 openclaw-gateway.service" | Out-Null
    Start-Sleep -Seconds 2
    $active = wsl -d Ubuntu-24.04 -e bash -c "systemctl is-active openclaw-gateway"
    if ($active.Trim() -eq "active") {
        Write-Host "  ✓ OpenClaw active" -ForegroundColor Green
    } else {
        Write-Warning "  OpenClaw status: $active — may need full restart"
    }
}

function Restart-Bridge {
    Write-Host "  → restarting bridge.py..." -ForegroundColor Yellow
    $bridgePid = (netstat -ano | Select-String ":8098 " | Select-String "LISTENING" | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1)
    if ($bridgePid) { taskkill /F /PID $bridgePid 2>&1 | Out-Null }
    Start-Sleep -Seconds 1
    Start-Process -FilePath $Py -ArgumentList "$RepoRoot\sentinel-miniapp-v2\bridge.py" -WorkingDirectory "$RepoRoot\sentinel-miniapp-v2" -WindowStyle Hidden
    Start-Sleep -Seconds 4
    $listening = netstat -ano | Select-String ":8098 " | Select-String "LISTENING"
    if ($listening) { Write-Host "  ✓ bridge.py listening on :8098" -ForegroundColor Green }
    else { Write-Warning "  bridge.py not listening on :8098" }
}

function Restart-Watchdog {
    Write-Host "  → restarting watchdog.py..." -ForegroundColor Yellow
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*watchdog.py*" } |
        ForEach-Object { taskkill /F /PID $_.ProcessId 2>&1 | Out-Null }
    Start-Sleep -Seconds 1
    $pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $pythonw) { $pythonw = $Py }
    Start-Process -FilePath $pythonw -ArgumentList "$RepoRoot\watchdog\watchdog.py" -WorkingDirectory "$RepoRoot\watchdog" -WindowStyle Hidden
    Write-Host "  ✓ watchdog.py restarted" -ForegroundColor Green
}

# ────────────────────────────────────────────────────────────────────────────

function List-Secrets {
@"
Sentinel secret rotation reference

Pure CLI (run: .\scripts\rotate.ps1 <name> <new-value>):
  telegram-ai          AI bot token (@YourSentinelBot)
  telegram-watchdog    Watchdog/middleware bot token
  telegram-testbot     ClaudeAssistant testbot token
  tavily               Tavily web-search API key
  azure-speech         Azure Speech key
  metamcp              MetaMCP bearer token
  lmstudio             LM Studio API key
  gateway-auth         OpenClaw gateway web-UI auth token (auto-gen if value omitted)
  github-pat           GitHub Personal Access Token
  totp                 Mini-app TOTP secret (regen + you scan new QR)
  cloudflare-tunnel    cloudflared tunnel rotate (no value needed; uses tunnel CLI)

Interactive (run: .\scripts\rotate.ps1 <name>  — prints instructions):
  telethon             User-account session (needs SMS code)
  google-oauth         Google Workspace MCP refresh tokens (needs browser)
  microsoft-oauth      OneDrive MCP refresh tokens (needs browser)
"@
}

function Print-InteractiveInstructions {
    param([string]$Name)
    switch ($Name) {
        "telethon" {
            Write-Host @"
Telethon session rotation requires interactive auth (your phone, SMS code).

Run this in a Python REPL:

  from telethon import TelegramClient
  from telethon.sessions import StringSession
  import keyring
  api_id   = int(keyring.get_password('telethon_api_id',   'telethon_api_id'))
  api_hash = keyring.get_password('telethon_api_hash', 'telethon_api_hash')
  with TelegramClient(StringSession(), api_id, api_hash) as c:
      c.start()  # prompts for phone number, then SMS code
      print(c.session.save())  # COPY THIS, do not share

Then save the printed string to WCM:

  .\scripts\rotate.ps1 telethon-session-string <the-new-string>

(use the literal subcommand 'telethon-session-string' — different from 'telethon')
"@
        }
        "google-oauth" {
            Write-Host @"
Google OAuth needs a browser flow.

1. Visit https://myaccount.google.com/permissions
2. Find 'Sentinel Google Workspace MCP' (or similar) → Remove access
3. Open http://localhost:8089/auth (the google-workspace-mcp container exposes this)
4. Sign in, grant scopes
5. New refresh tokens auto-saved to the container's volume

Smoke test: ask Sentinel 'summarize my recent emails' or 'what's on my calendar today'
"@
        }
        "microsoft-oauth" {
            Write-Host @"
Microsoft OAuth needs a browser flow.

1. Visit https://account.microsoft.com/privacy/app-access — revoke OneDrive MCP app
2. Open http://localhost:8093/auth (the onedrive-mcp container exposes this)
3. Sign in, grant scopes
4. New refresh tokens auto-saved

Smoke test: ask Sentinel 'list my OneDrive files'
"@
        }
    }
}

# ────────────────────────────────────────────────────────────────────────────

if ($Secret -eq "list" -or $Secret -eq "help") {
    Write-Host (List-Secrets)
    exit 0
}

# Interactive secrets — print instructions, don't try to do anything
if ($Secret -in @("telethon", "google-oauth", "microsoft-oauth")) {
    Print-InteractiveInstructions $Secret
    exit 0
}

# Special: gateway-auth can auto-generate
if ($Secret -eq "gateway-auth" -and -not $NewValue) {
    Write-Host "  → auto-generating new gateway auth token (48 hex chars)..." -ForegroundColor Yellow
    $NewValue = & $Py -c "import secrets; print(secrets.token_hex(24))"
    Write-Host "  ✓ token generated (not displayed; written to openclaw.json)" -ForegroundColor Green
}

# Special: cloudflare-tunnel doesn't take a value — runs the tunnel rotation
if ($Secret -eq "cloudflare-tunnel") {
    Write-Host "Cloudflare Tunnel rotation (manual approval required for some steps):"
    Write-Host "  1. cloudflared tunnel delete sentinel"
    Write-Host "  2. cloudflared tunnel create sentinel-2"
    Write-Host "  3. cloudflared tunnel route dns sentinel-2 sentinel.your-domain.example.com"
    Write-Host "  4. Update C:\Users\<you>\.cloudflared\config.yml to point at the new tunnel UUID"
    Write-Host "  5. Restart cloudflared service"
    Write-Host ""
    Write-Host "These need your input (delete confirms, DNS routes, etc.) so I'm not auto-running them."
    exit 0
}

# Special: totp regenerates without taking a value
if ($Secret -eq "totp") {
    Write-Host "  → bridge.py will regenerate the TOTP secret on next start; new QR will be at" -ForegroundColor Yellow
    Write-Host "     C:\Users\$env:USERNAME\metamcp-local\sentinel-miniapp-v2\totp_setup.html"
    Write-Host "     Open it in a browser, scan with Authenticator app, then re-login to mini-app."
    Write-Host ""
    # Delete the existing TOTP entry from WCM so bridge regenerates
    & $Py -c "import keyring; keyring.delete_password('totp_secret', 'totp_secret')" 2>&1 | Out-Null
    Restart-Bridge
    Write-Host "  ✓ TOTP secret cleared, bridge restarted — open totp_setup.html now" -ForegroundColor Green
    exit 0
}

# All remaining secrets need a value
if (-not $NewValue) {
    Write-Error "Need a value: .\scripts\rotate.ps1 $Secret <new-value>"
    Write-Host "(Run '.\scripts\rotate.ps1 list' to see all options)"
    exit 1
}

# ────────────────────────────────────────────────────────────────────────────
# Per-secret rotation logic

switch ($Secret) {

    "telegram-ai" {
        Write-Host "Rotating @YourSentinelBot token..." -ForegroundColor Cyan
        Set-WcmSecret -Service "telegram_bot_token" -User "telegram_bot_token" -Value $NewValue
        Update-OpenclawJson -JqPath ".channels.telegram.botToken" -Value $NewValue
        Restart-OpenclawGateway
        Write-Host "Done. Smoke test: send a message to @YourSentinelBot in Telegram." -ForegroundColor Green
    }

    "telegram-watchdog" {
        Write-Host "Rotating watchdog/middleware bot token..." -ForegroundColor Cyan
        Set-WcmSecret -Service "sentinel-watchdog" -User "bot_token" -Value $NewValue
        Restart-Watchdog
        Write-Host "Done. Smoke test: trigger watchdog DNS check or wait for next status ping." -ForegroundColor Green
    }

    "telegram-testbot" {
        Write-Host "Rotating @SentinelClaudeAssistantBot (testbot) token..." -ForegroundColor Cyan
        $envFile = Join-Path $env:USERPROFILE ".claude\projects\Projects-Proposal-WIP\V4\ClaudeAssistant\.env.testenv"
        if (-not (Test-Path $envFile)) { Write-Error "env file not found: $envFile"; exit 1 }
        $content = Get-Content $envFile -Raw
        $newContent = $content -replace "(?m)^TESTBOT_TOKEN=.*$", "TESTBOT_TOKEN=$NewValue"
        Set-Content -Path $envFile -Value $newContent -NoNewline
        Write-Host "  ✓ .env.testenv updated" -ForegroundColor Green
        Write-Host "Done. Smoke test: .\scripts\notify_owner.ps1 -Message 'rotation ok'" -ForegroundColor Green
    }

    "tavily" {
        Write-Host "Rotating Tavily API key..." -ForegroundColor Cyan
        Update-OpenclawJson -JqPath ".plugins.entries.tavily.config.webSearch.apiKey" -Value $NewValue
        Restart-OpenclawGateway
        Write-Host "Done. Smoke test: ask Sentinel 'search the web for X'." -ForegroundColor Green
    }

    "azure-speech" {
        Write-Host "Rotating Azure Speech key..." -ForegroundColor Cyan
        Update-OpenclawJson -JqPath ".talk.providers.azure-speech.apiKey" -Value $NewValue
        Restart-OpenclawGateway
        Write-Host "Done. Smoke test: trigger TTS reply." -ForegroundColor Green
    }

    "metamcp" {
        Write-Host "Rotating MetaMCP bearer token..." -ForegroundColor Cyan
        Set-WcmSecret -Service "metamcp_bearer_token" -User "metamcp_bearer_token" -Value $NewValue
        Update-OpenclawJson -JqPath ".mcp.servers.metamcp.headers.Authorization" -Value "Bearer $NewValue"
        Restart-OpenclawGateway
        Restart-Bridge
        Write-Host "Done. Smoke test: ask Sentinel to use any MCP tool ('what's the weather?')." -ForegroundColor Green
    }

    "lmstudio" {
        Write-Host "Rotating LM Studio API key..." -ForegroundColor Cyan
        Update-OpenclawJson -JqPath ".models.providers.lmstudio.apiKey" -Value $NewValue
        Restart-OpenclawGateway
        Write-Host "Done. Smoke test: any chat with Sentinel will fail loudly if this is wrong." -ForegroundColor Green
    }

    "gateway-auth" {
        Write-Host "Rotating OpenClaw gateway auth token..." -ForegroundColor Cyan
        Update-OpenclawJson -JqPath ".gateway.auth.token" -Value $NewValue
        Restart-OpenclawGateway
        Write-Host "Done. Smoke test: open http://127.0.0.1:18789 — should require new token." -ForegroundColor Green
    }

    "github-pat" {
        Write-Host "Rotating GitHub PAT..." -ForegroundColor Cyan
        Set-WcmSecret -Service "github_pat" -User "github_pat" -Value $NewValue
        Write-Host "  → restarting github-mcp container..." -ForegroundColor Yellow
        docker restart github-mcp 2>&1 | Out-Null
        Write-Host "  ✓ github-mcp container restarted" -ForegroundColor Green
        Write-Host "Done. Smoke test: ask Sentinel 'list my recent GitHub issues'." -ForegroundColor Green
    }

    "telethon-session-string" {
        # Special subcommand for after the user has run the interactive Python flow
        Write-Host "Storing new Telethon session string..." -ForegroundColor Cyan
        Set-WcmSecret -Service "telethon_session" -User "telethon_session" -Value $NewValue
        Restart-Bridge
        Write-Host "Done. Smoke test: chat composer in mini-app should work." -ForegroundColor Green
    }

    default {
        Write-Error "Unknown secret name: '$Secret'"
        Write-Host "Run '.\scripts\rotate.ps1 list' for available options"
        exit 1
    }
}
