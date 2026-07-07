# scripts/rotate_docintel.ps1
#
# Rotates the Azure Document Intelligence (formerly Form Recognizer) key
# used by onedrive-mcp for PDF parsing. Key lives in WCM, env-substituted
# into the onedrive-mcp container at startup.
#
# Pre-req: regenerate Key1 at Azure Portal:
#   https://portal.azure.com/#blade/HubsExtension/BrowseAll/resourceType/Microsoft.CognitiveServices%2Faccounts
#   -> click your Document Intelligence resource -> "Keys and Endpoint" -> "Regenerate Key1"
#
# Usage:  .\scripts\rotate_docintel.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

Write-Host ""
Write-Host "── Rotate Azure Document Intelligence key ──" -ForegroundColor Cyan
Write-Host "Paste the new Key1 from Azure portal. Input is hidden." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "New key" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 20) {
    Write-Host "[x] Key looks too short - aborting" -ForegroundColor Red
    exit 1
}

# Store in WCM
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','docintel_key','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/docintel_key" -ForegroundColor Green

# Resync .env.local
Write-Host ""
Write-Host "Re-syncing .env.local from WCM..." -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

# Restart onedrive-mcp so it picks up the new env
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

Write-Host ""
Write-Host "[ok] Done. Next PDF-parse call routes through the new key." -ForegroundColor Green
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue
