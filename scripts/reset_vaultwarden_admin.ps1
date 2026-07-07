# scripts/reset_vaultwarden_admin.ps1
#
# Two-step admin-token reset:
#   STEP 1 (manual, do this BEFORE running this script):
#     docker exec -it vaultwarden /vaultwarden hash --preset bitwarden
#     -> type a password you'll remember, confirm, copy the $argon2id$... string
#
#   STEP 2 (this script):
#     Paste the $argon2id$... hash at the prompt. Script stores it in WCM,
#     re-syncs .env.local, restarts vaultwarden.
#
# Usage:  .\scripts\reset_vaultwarden_admin.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

Write-Host ""
Write-Host "── Install Vaultwarden admin hash ──" -ForegroundColor Cyan
Write-Host "Paste the entire \$argon2id\$... hash you got from the hash command." -ForegroundColor Yellow
Write-Host "(Just the hash, no surrounding quotes or 'ADMIN_TOKEN=' prefix.)" -ForegroundColor DarkGray
Write-Host ""

$sec = Read-Host "Hash" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$hash = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

# Trim possible decorations from copy-paste
$hash = $hash.Trim().Trim("'").Trim('"')
if ($hash -match "^ADMIN_TOKEN='?(.+?)'?$") { $hash = $Matches[1] }

if (-not $hash.StartsWith('$argon2id$')) {
    Write-Host "[x] Hash doesn't start with `$argon2id`$ - did you copy the wrong piece?" -ForegroundColor Red
    Write-Host "    Expected format: `$argon2id`$v=19`$m=65536,t=3,p=4`$...salt...`$...hash..." -ForegroundColor DarkGray
    exit 1
}
Write-Host "  [ok] hash recognized ($($hash.Length) chars)" -ForegroundColor Green

# Store in WCM via temp file (avoids exposing on command line)
$tempH = Join-Path $env:TEMP "new_vw_hash.txt"
Set-Content -Path $tempH -Value $hash -NoNewline -Encoding UTF8
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','vaultwarden_admin_token', open(r'$tempH', encoding='utf-8-sig').read().strip())"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Remove-Item -Path $tempH -ErrorAction SilentlyContinue
Write-Host "  [ok] WCM updated: sentinel-miniapp/vaultwarden_admin_token" -ForegroundColor Green

Write-Host ""
Write-Host "Re-syncing .env.local from WCM..." -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting vaultwarden (full down/rm/up)..." -ForegroundColor Cyan
    docker stop vaultwarden 2>&1 | Out-Null
    docker rm vaultwarden 2>&1 | Out-Null
    docker compose --env-file (Join-Path $RepoRoot ".env.local") `
        -f (Join-Path $RepoRoot "docker-compose.yml") `
        up -d vaultwarden 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $prev
}

Start-Sleep -Seconds 4
Write-Host -NoNewline "  vaultwarden /alive: "
curl.exe -s -m 5 -o $null -w "HTTP=%{http_code}`n" http://127.0.0.1:8085/alive

Write-Host ""
Write-Host "[ok] Done. Log into http://127.0.0.1:8085/admin with the PLAINTEXT password you typed at the hash prompt." -ForegroundColor Green
