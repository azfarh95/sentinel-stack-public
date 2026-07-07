# scripts/rotate_github_pat.ps1
#
# Rotates the GitHub Personal Access Token used by github-mcp. Validates
# via /user before storing. Restarts github-mcp container so it picks up
# the new value from .env.local.
#
# Pre-req: regenerate at GitHub:
#   Classic:      https://github.com/settings/tokens
#                 -> click your token -> "Regenerate token"
#                 (scopes typically: repo, read:org, gist)
#   Fine-grained: https://github.com/settings/personal-access-tokens
#                 -> click your token -> "Regenerate token"
# Either type works - github-mcp uses Bearer auth which accepts both.
#
# Usage:  .\scripts\rotate_github_pat.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

Write-Host ""
Write-Host "── Rotate GitHub PAT ──" -ForegroundColor Cyan
Write-Host "Paste the new token (starts ghp_, gho_, ghs_, or github_pat_). Input is hidden." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "New token" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 20) {
    Write-Host "[x] Token looks too short - aborting" -ForegroundColor Red
    exit 1
}

# 1. Validate via /user. Use Invoke-RestMethod (PowerShell + curl arg-parsing
#    issues with quoted headers).
Write-Host ""
Write-Host "Validating via api.github.com/user..." -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod -Method Get `
        -Uri "https://api.github.com/user" `
        -Headers @{
            "Authorization" = "Bearer $tok"
            "Accept"        = "application/vnd.github+json"
            "User-Agent"    = "sentinel-rotate-script"
        } `
        -TimeoutSec 10 -ErrorAction Stop
    Write-Host "  [ok] Token validated: @$($resp.login)  (id=$($resp.id))" -ForegroundColor Green
} catch {
    Write-Host "[x] GitHub rejected the token: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# Confirm scopes (different from token format check - tells you if scopes are
# enough for repo operations). Use IRM head call: GitHub returns granted
# scopes in the X-OAuth-Scopes response header.
try {
    $headResp = Invoke-WebRequest -Method Get `
        -Uri "https://api.github.com/user" `
        -Headers @{
            "Authorization" = "Bearer $tok"
            "User-Agent"    = "sentinel-rotate-script"
        } -TimeoutSec 8
    $scopes = $headResp.Headers["X-OAuth-Scopes"]
    if ($scopes) {
        Write-Host "  granted scopes: $scopes" -ForegroundColor Gray
    } else {
        Write-Host "  granted scopes: (fine-grained PAT - no header)" -ForegroundColor Gray
    }
} catch {
    # non-fatal
}

# 2. WCM
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','github_pat','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/github_pat" -ForegroundColor Green

# 3. Re-sync env
Write-Host ""
Write-Host "Re-syncing .env.local from WCM..." -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

# 4. Restart github-mcp
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting github-mcp..." -ForegroundColor Cyan
    docker compose --env-file (Join-Path $RepoRoot ".env.local") `
        -f (Join-Path $RepoRoot "docker-compose.yml") `
        up -d --no-deps --force-recreate github-mcp 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $prev
}

# 5. Health check
Write-Host ""
Start-Sleep -Seconds 3
Write-Host -NoNewline "github-mcp loopback: "
curl.exe -s -m 5 -o $null -w "HTTP=%{http_code}`n" http://127.0.0.1:8091/health

Write-Host ""
Write-Host "[ok] Done. Old PAT is now revocable in GitHub settings." -ForegroundColor Green
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue
