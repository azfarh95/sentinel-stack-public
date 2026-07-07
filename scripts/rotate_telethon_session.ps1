# scripts/rotate_telethon_session.ps1
#
# Rotates the Telethon user-account session string used by smdl for
# >50 MB MTProto uploads (and previously by sentinel-miniapp for
# chat-composer). Unique vs other rotations because TELEGRAM itself is
# the authenticator - not a portal. You will be SMS'd a login code.
#
# Pre-req: have your phone in hand. The bound number is your personal
# Telegram account (NOT the bot). If 2FA cloud password is enabled on
# the account, have that ready too.
#
# What this does:
#   1. Reads api_id + api_hash from WCM (already set up; created at my.telegram.org)
#   2. Drops you into Telethon's interactive auth: phone -> SMS code -> 2FA
#   3. On success, captures the StringSession output and stores it in WCM
#   4. Re-syncs .env.local
#   5. Restarts smdl container
#
# Usage:  .\scripts\rotate_telethon_session.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

Write-Host ""
Write-Host "── Rotate Telethon user-account session ──" -ForegroundColor Cyan
Write-Host "You will be SMS'd a login code by Telegram." -ForegroundColor Yellow
Write-Host "If you have 2FA cloud password on the account, you'll be prompted for it too." -ForegroundColor Yellow
Write-Host ""

# Interactive Python auth - Telethon handles all prompts (phone, code, password)
# and writes the new session string to a temp file on success. Drop the python
# script to a temp .py file because piping a here-string into `python -` here
# would lose the interactive stdin needed for Telethon prompts.
$tempSess = Join-Path $env:TEMP "new_telethon_session.txt"
$tempPy   = Join-Path $env:TEMP "telethon_auth.py"
Remove-Item -Path $tempSess -ErrorAction SilentlyContinue

$pyCode = @'
import keyring, sys, os
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id_str = keyring.get_password('sentinel-miniapp', 'telethon_api_id')
api_hash   = keyring.get_password('sentinel-miniapp', 'telethon_api_hash')
if not api_id_str or not api_hash:
    print('[x] api_id / api_hash missing from WCM. Set them via my.telegram.org first.', file=sys.stderr)
    sys.exit(1)

api_id = int(api_id_str)
temp_path = os.path.expandvars(r'%TEMP%\new_telethon_session.txt')

print('Launching Telethon interactive auth...')
print('Enter your phone number (incl. country code, e.g. +65...) when prompted.')
print()

# .start() drives phone -> code -> 2FA password prompts on stdin.
with TelegramClient(StringSession(), api_id, api_hash) as client:
    me = client.get_me()
    sess = client.session.save()
    print()
    print(f'[ok] Authenticated as: {me.first_name} (@{me.username or "no-username"}, id={me.id})')
    with open(temp_path, 'w') as f:
        f.write(sess)
    print(f'[ok] Session captured ({len(sess)} chars)')
'@

Set-Content -Path $tempPy -Value $pyCode -Encoding UTF8

try {
    & $Py $tempPy
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[x] Auth flow aborted (exit $LASTEXITCODE)." -ForegroundColor Red
        exit 1
    }
} finally {
    Remove-Item -Path $tempPy -ErrorAction SilentlyContinue
}

if (-not (Test-Path $tempSess)) {
    Write-Host "[x] No session captured - aborting." -ForegroundColor Red
    exit 1
}

# Store in WCM via python reading temp file (session string never crosses the
# PS command line, so it's not visible to screen-share or shell history).
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','telethon_session', open(r'$tempSess').read().strip())"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/telethon_session" -ForegroundColor Green

# Re-sync env
Write-Host ""
Write-Host "Re-syncing .env.local from WCM..." -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

# Restart smdl (sole consumer right now)
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting smdl..." -ForegroundColor Cyan
    docker compose --env-file (Join-Path $RepoRoot ".env.local") `
        -f (Join-Path $RepoRoot "docker-compose.yml") `
        up -d --no-deps --force-recreate smdl 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $prev
}

# Wipe temp + local
Remove-Item -Path $tempSess -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[ok] Done. Send a >50 MB video to @AZ_SMDL_bot to confirm Telethon upload path works." -ForegroundColor Green
Write-Host ""
Write-Host "Side-effect note:" -ForegroundColor DarkGray
Write-Host "  All other Telethon sessions for this account got invalidated when you" -ForegroundColor DarkGray
Write-Host "  authenticated. Telegram will show this in Active Sessions on your phone." -ForegroundColor DarkGray
