﻿﻿# scripts/rotate_smdl_bot.ps1
#
# One-shot helper to rotate the SMDL bot token without it appearing in
# any chat log or shell history.
#
# Usage (interactive):
#   .\scripts\rotate_smdl_bot.ps1
#
# What it does:
#   1. Prompts for the new token via Read-Host -AsSecureString (no echo)
#   2. Validates the token by calling Telegram's getMe
#   3. Stores it in WCM under sentinel-smdl/bot_token
#   4. Re-syncs .env.local from WCM
#   5. Restarts the smdl container
#   6. Configures bot first_name + canonical command list via Bot API
#   7. Prints the bot's @username + name (no token re-print)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Py)) { $Py = (Get-Command py).Source }

Write-Host ""
Write-Host "── Rotate SMDL bot token ──────────────────────────────" -ForegroundColor Cyan
Write-Host "Paste the token from BotFather. Input is hidden." -ForegroundColor Yellow
Write-Host ""

# 1. Read token securely
$secureTok = Read-Host "New token" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureTok)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 30) {
    Write-Host "✗ Token looks too short — aborting" -ForegroundColor Red
    exit 1
}

# 2. Validate via getMe
Write-Host ""
Write-Host "Validating token via getMe…" -ForegroundColor Cyan
$meRaw = curl.exe -s "https://api.telegram.org/bot$tok/getMe"
$me = $meRaw | ConvertFrom-Json
if (-not $me.ok) {
    Write-Host "✗ Token rejected by Telegram: $meRaw" -ForegroundColor Red
    exit 1
}
$username = $me.result.username
$name     = $me.result.first_name
Write-Host "  ✓ Bot validated: @$username  (`"$name`")" -ForegroundColor Green

# 3. Stash in WCM. sync_env_from_wcm.ps1 reads from sentinel-miniapp,
#    not sentinel-smdl — first version of this script had the wrong
#    namespace and we caught it manually. Canonical location:
#    sentinel-miniapp/smdl_bot_token.
Write-Host ""
Write-Host "Storing in Windows Credential Manager…" -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','smdl_bot_token','$tok')"
if ($LASTEXITCODE -ne 0) { throw "keyring.set_password failed" }
Write-Host "  ✓ WCM updated: sentinel-miniapp/smdl_bot_token" -ForegroundColor Green

# 4. Resync .env.local
Write-Host ""
Write-Host "Re-syncing .env.local from WCM…" -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

# 5. Restart smdl container.
# Note: docker compose writes warnings to stderr (e.g. "argon2id variable
# not set" from Vaultwarden token's $-interpolation). PowerShell's
# $ErrorActionPreference="Stop" would normally abort the script on those.
# Wrap inside a $ErrorActionPreference="Continue" block so cosmetic
# stderr doesn't kill the rotation mid-way.
$prevErrPref = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting smdl container…" -ForegroundColor Cyan
    $composeOut = docker compose --env-file (Join-Path $RepoRoot ".env.local") `
        -f (Join-Path $RepoRoot "docker-compose.yml") `
        up -d --no-deps --force-recreate smdl 2>&1
    $composeOut | Select-Object -Last 5 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $prevErrPref
}

# 6. Configure bot via API: rename + canonical command list + clear stale commands
Write-Host ""
Write-Host "Configuring new bot (name + commands)…" -ForegroundColor Cyan
$nameToSet = "Az - SMDL"
$null = curl.exe -s "https://api.telegram.org/bot$tok/setMyName" -d "name=$nameToSet"

# Clear stale command lists across known scopes
$null = curl.exe -s "https://api.telegram.org/bot$tok/deleteMyCommands"
Add-Type -AssemblyName System.Web
$privScope = [System.Web.HttpUtility]::UrlEncode('{"type":"all_private_chats"}')
$null = curl.exe -s "https://api.telegram.org/bot$tok/deleteMyCommands" -d "scope=$privScope"

$cmds = @(
  @{command="watch";          description="Add a streamer URL to the live-watch list"}
  @{command="unwatch";        description="Remove a streamer URL from the watch list"}
  @{command="watchlist";      description="Show the current watch list + status"}
  @{command="live_status";    description="Show the active livestream recording (if any)"}
  @{command="stop_livestream"; description="Halt the current livestream recording"}
) | ConvertTo-Json -Compress
$encoded = [System.Web.HttpUtility]::UrlEncode($cmds)
$null = curl.exe -s "https://api.telegram.org/bot$tok/setMyCommands" -d "commands=$encoded"

# Description (long press → About)
$desc = "Standalone media downloader. Send any video URL — TikTok, YouTube, Instagram, Twitter, Reddit, Twitch, Kick. Records livestreams with /watch."
$encDesc = [System.Web.HttpUtility]::UrlEncode($desc)
$null = curl.exe -s "https://api.telegram.org/bot$tok/setMyDescription" -d "description=$encDesc"

$short = "yt-dlp + gallery-dl downloader. Send a URL."
$encShort = [System.Web.HttpUtility]::UrlEncode($short)
$null = curl.exe -s "https://api.telegram.org/bot$tok/setMyShortDescription" -d "short_description=$encShort"

# 7. Verify final state
Write-Host ""
Write-Host "── Final verification ─────────────────────────────────" -ForegroundColor Cyan
$verifyMe   = (curl.exe -s "https://api.telegram.org/bot$tok/getMe") | ConvertFrom-Json
$verifyName = (curl.exe -s "https://api.telegram.org/bot$tok/getMyName") | ConvertFrom-Json
$verifyCmds = (curl.exe -s "https://api.telegram.org/bot$tok/getMyCommands") | ConvertFrom-Json

Write-Host "  username  : @$($verifyMe.result.username)"
Write-Host "  name      : $($verifyName.result.name)"
Write-Host "  commands  : $($verifyCmds.result.Count) registered"
$verifyCmds.result | ForEach-Object { Write-Host "              /$($_.command) — $($_.description)" }

Write-Host ""
Write-Host "✓ Done. Send a URL to @$($verifyMe.result.username) to test." -ForegroundColor Green
Write-Host ""
Write-Host "If you also want to retire the old @YourSMDLBot, do that in BotFather → /deletebot." -ForegroundColor DarkGray

# Wipe local var (best-effort — PowerShell doesn't guarantee zeroing)
$tok = "x" * 100
Remove-Variable tok -ErrorAction SilentlyContinue
