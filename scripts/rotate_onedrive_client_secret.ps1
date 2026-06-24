# scripts/rotate_onedrive_client_secret.ps1
#
# Rotates the OneDrive (Azure AD App Registration) client secret used by
# onedrive-mcp. App secrets are mutable in WCM; the OAuth refresh token
# already in the container's cache stays valid across secret rotations
# (refresh tokens are tied to the user grant, not the app secret).
#
# Pre-req: generate a new secret in Azure Portal:
#   https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade
#   -> your app -> "Certificates & secrets" -> "+ New client secret"
#   -> recommended lifetime: 12 months. Copy the Value (not the ID).
#   You can leave the old secret active until next refresh, then delete.
#
# Usage:  .\scripts\rotate_onedrive_client_secret.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

Write-Host ""
Write-Host "── Rotate OneDrive client secret ──" -ForegroundColor Cyan
Write-Host "Paste the new secret VALUE (not ID) from Azure. Input is hidden." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "New secret" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 30) {
    Write-Host "[x] Secret looks too short - aborting (Azure secrets are typically 40+ chars)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','onedrive_client_secret','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/onedrive_client_secret" -ForegroundColor Green

Write-Host ""
Write-Host "Re-syncing .env.local from WCM..." -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting onedrive-mcp..." -ForegroundColor Cyan
    docker compose --env-file (Join-Path $RepoRoot ".env.local") `
        -f (Join-Path $RepoRoot "docker-compose.yml") `
        up -d --no-deps --force-recreate onedrive-mcp 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $prev
}

# Optional health probe: the MCP itself returns 200 if Flask boots, but a deeper
# probe requires a real OneDrive call. Just check the HTTP endpoint is up.
Write-Host ""
Write-Host "── Quick health check ──" -ForegroundColor Cyan
Start-Sleep -Seconds 4
Write-Host -NoNewline "onedrive-mcp loopback: "
curl.exe -s -m 5 -o $null -w "HTTP=%{http_code}`n" http://127.0.0.1:8093/health

Write-Host ""
Write-Host "[ok] Done. Old secret can be deleted from Azure once first call succeeds." -ForegroundColor Green
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue
