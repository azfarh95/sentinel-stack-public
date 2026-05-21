# scripts/rotate_better_auth_secret.ps1
#
# Rotates BETTER_AUTH_SECRET (MetaMCP session encryption). Internal value
# only - no external portal. Auto-generates a fresh 32-byte base64 secret
# locally, stores in WCM, restarts MetaMCP.
#
# Side effect: all existing MetaMCP sessions are invalidated. Any client
# (LM Studio, OpenClaw) using a long-lived API key keeps working - those
# are stored hashed and re-validated on each request, not session-bound.
#
# Usage:  .\scripts\rotate_better_auth_secret.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

Write-Host ""
Write-Host "── Rotate BETTER_AUTH_SECRET ──" -ForegroundColor Cyan
Write-Host "No portal step needed - generating locally." -ForegroundColor Yellow

# 1. Generate a fresh 32-byte URL-safe base64 secret
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$tok = [Convert]::ToBase64String($bytes)

Write-Host ""
Write-Host "  generated (preview): ...$($tok.Substring($tok.Length-6))" -ForegroundColor Gray

# 2. Store in WCM
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','better_auth_secret','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/better_auth_secret" -ForegroundColor Green

# 3. Re-sync env
Write-Host ""
Write-Host "Re-syncing .env.local from WCM..." -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

# 4. Restart MetaMCP
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting MetaMCP..." -ForegroundColor Cyan
    docker compose --env-file (Join-Path $RepoRoot ".env.local") `
        -f (Join-Path $RepoRoot "docker-compose.yml") `
        up -d --no-deps --force-recreate metamcp 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $prev
}

Write-Host ""
Write-Host "[ok] Done. All MetaMCP web sessions invalidated; API keys still work." -ForegroundColor Green
Write-Host "    If you were logged into http://localhost:12008 in a browser, log in again." -ForegroundColor DarkGray
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue
