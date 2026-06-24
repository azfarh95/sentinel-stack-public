﻿# scripts/rotate_agent_bot.ps1
#
# Rotates the @YourSentinelBot token (the main agent bot consumed by
# OpenClaw gateway). Same secure-prompt pattern as rotate_smdl_bot —
# input never lands in shell history or this chat's log.
#
# Pre-req: /revoke this bot in BotFather first, then have the new token
#          ready to paste at the hidden prompt.
#
# Usage:  .\scripts\rotate_agent_bot.ps1

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Py)) { $Py = (Get-Command py).Source }

Write-Host ""
Write-Host "── Rotate YourSentinelBot (agent) token ──" -ForegroundColor Cyan
Write-Host "Paste the new token from BotFather. Input is hidden." -ForegroundColor Yellow
Write-Host ""

# 1. Read securely
$sec = Read-Host "New token" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 30) {
    Write-Host "✗ Token looks too short — aborting" -ForegroundColor Red
    exit 1
}

# 2. Validate via getMe
Write-Host ""
Write-Host "Validating token via getMe..." -ForegroundColor Cyan
$meRaw = curl.exe -s "https://api.telegram.org/bot$tok/getMe"
$me = $meRaw | ConvertFrom-Json
if (-not $me.ok) {
    Write-Host "✗ Token rejected by Telegram: $meRaw" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ Bot validated: @$($me.result.username)  (`"$($me.result.first_name)`")" -ForegroundColor Green

# 3. Update WCM mirror (for sync_env_from_wcm.ps1 + any other consumer)
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','telegram_bot_token','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  ✓ WCM updated: sentinel-miniapp/telegram_bot_token" -ForegroundColor Green

# 4. Update openclaw.json (OpenClaw reads from here, NOT from env vars).
# Stage the token via a temp file rather than nested shell quoting —
# the previous PowerShell -> wsl -> bash -> python -c -> single-quoted-token
# chain broke silently on rotation, leaving openclaw.json stale while
# WCM updated. Temp file is wiped after the WSL write.
Write-Host ""
Write-Host "Updating openclaw.json botToken..." -ForegroundColor Cyan
$tempTok = Join-Path $env:TEMP "new_agent_token.txt"
Set-Content -Path $tempTok -Value $tok -NoNewline -Encoding UTF8
try {
    wsl -d Ubuntu-24.04 -u azfar --exec bash -c "python3 - << 'PYEOF'
import json
with open('/mnt/c/Users/azfar/AppData/Local/Temp/new_agent_token.txt') as f:
    tok = f.read().strip()
with open('/home/azfar/.openclaw/openclaw.json') as f: cfg = json.load(f)
cfg.setdefault('channels', {}).setdefault('telegram', {})['botToken'] = tok
with open('/home/azfar/.openclaw/openclaw.json', 'w') as f: json.dump(cfg, f, indent=2)
print(f'  openclaw.json updated (ends ...{tok[-6:]})')
PYEOF"
} finally {
    Remove-Item -Path $tempTok -ErrorAction SilentlyContinue
}

# 5. Restart openclaw-gateway service. systemctl writes to stderr which
# would normally trip ErrorActionPreference=Stop. Wrap in Continue.
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting openclaw-gateway..." -ForegroundColor Cyan
    wsl -d Ubuntu-24.04 -u root --exec bash -c "systemctl restart openclaw-gateway && sleep 6 && systemctl is-active openclaw-gateway"
} finally {
    $ErrorActionPreference = $prev
}

# 6. Verify the new bot is alive on OpenClaw side
Write-Host ""
Write-Host "── Verification ──" -ForegroundColor Cyan
$verify = (curl.exe -s "https://api.telegram.org/bot$tok/getMe") | ConvertFrom-Json
Write-Host "  username     : @$($verify.result.username)"
Write-Host "  name         : $($verify.result.first_name)"
Write-Host "  bot ID       : $($verify.result.id)"
Write-Host ""
Write-Host "  Tail of openclaw-gateway log (should show 'starting provider'):" -ForegroundColor Gray
wsl -d Ubuntu-24.04 -u root --exec bash -c "journalctl -u openclaw-gateway --no-pager --since '20 seconds ago' 2>&1 | grep -iE 'telegram.*starting|sendMessage' | tail -3"

# Wipe local
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "✓ Done. Send @YourSentinelBot a test message to confirm round-trip." -ForegroundColor Green
