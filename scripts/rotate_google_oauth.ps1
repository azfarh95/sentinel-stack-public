# scripts/rotate_google_oauth.ps1
#
# Rotates the Google Workspace OAuth client_secret used by
# google-workspace-mcp. Refresh tokens stay valid (Google's refresh
# exchange identifies by client_id + refresh_token; the new
# client_secret just needs to match the new server-side state).
#
# Updates TWO files:
#   1. data\credentials.json      web.client_secret
#   2. data\token.json            client_secret (Google's auth lib
#                                                 caches it here too)
#
# Validates by doing a real refresh-token exchange BEFORE writing - if
# Google rejects the secret, nothing on disk changes.
#
# Pre-req: rotate at Google Cloud Console:
#   https://console.cloud.google.com/apis/credentials?project=my-ai-workspace-494919
#   -> click your OAuth 2.0 Client ID -> "Reset Client Secret" (or
#   "Add Secret" for a grace period). Copy the new secret value.
#
# Usage:  .\scripts\rotate_google_oauth.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
$DataDir = Join-Path $RepoRoot "google-workspace-mcp\data"
$CredFile = Join-Path $DataDir "credentials.json"
$TokFile  = Join-Path $DataDir "token.json"

Write-Host ""
Write-Host "── Rotate Google Workspace OAuth client_secret ──" -ForegroundColor Cyan
Write-Host "Paste the new client_secret from Google Cloud Console. Input is hidden." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "New client_secret" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 20) {
    Write-Host "[x] Secret looks too short - aborting (Google secrets are 24+ chars)" -ForegroundColor Red
    exit 1
}

# 1. Validate via refresh-token exchange BEFORE touching disk
Write-Host ""
Write-Host "Validating via refresh-token exchange..." -ForegroundColor Cyan

$tempSecret = Join-Path $env:TEMP "new_google_secret.txt"
Set-Content -Path $tempSecret -Value $tok -NoNewline -Encoding UTF8

$validateScript = @'
import json, sys, urllib.request, urllib.parse, os
tok_file = sys.argv[1]
secret_file = sys.argv[2]
# utf-8-sig strips a leading BOM if present. PS 5.1's `Set-Content -Encoding
# UTF8` writes a BOM, so plain open() would read it as a literal char and
# Google returns invalid_client because the secret has ﻿ prepended.
with open(secret_file, encoding='utf-8-sig') as f: new_secret = f.read().strip()
with open(tok_file, encoding='utf-8-sig') as f: t = json.load(f)
data = urllib.parse.urlencode({
    "client_id": t["client_id"],
    "client_secret": new_secret,
    "refresh_token": t["refresh_token"],
    "grant_type": "refresh_token"
}).encode()
req = urllib.request.Request(t["token_uri"], data=data,
    headers={"Content-Type":"application/x-www-form-urlencoded"})
try:
    with urllib.request.urlopen(req, timeout=12) as resp:
        body = json.loads(resp.read())
        if not body.get("access_token"):
            print("[x] refresh exchange returned no access_token:", body, file=sys.stderr)
            sys.exit(2)
        print(f"[ok] refresh exchange returned access_token (expires in {body.get('expires_in','?')}s)")
        sys.exit(0)
except urllib.error.HTTPError as e:
    err = e.read().decode()
    print(f"[x] Google rejected the secret (HTTP {e.code}): {err}", file=sys.stderr)
    sys.exit(3)
'@
$tempValidate = Join-Path $env:TEMP "google_validate.py"
Set-Content -Path $tempValidate -Value $validateScript -Encoding UTF8

try {
    & $Py $tempValidate $TokFile $tempSecret
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    aborting - on-disk credentials unchanged" -ForegroundColor Red
        exit 1
    }
} finally {
    Remove-Item -Path $tempValidate -ErrorAction SilentlyContinue
}

# 2. Update both files atomically
Write-Host ""
Write-Host "Updating credentials.json + token.json..." -ForegroundColor Cyan

$updateScript = @'
import json, sys
cred_file, tok_file, secret_file = sys.argv[1:4]
# utf-8-sig: see note in validate script. PS 5.1 writes BOM that would
# otherwise embed as ﻿ in the JSON string field.
with open(secret_file, encoding='utf-8-sig') as f: new_secret = f.read().strip()

with open(cred_file, encoding='utf-8-sig') as f: cred = json.load(f)
cred["web"]["client_secret"] = new_secret
with open(cred_file, "w", encoding='utf-8') as f: json.dump(cred, f, indent=2)
print(f"  credentials.json updated (ends ...{new_secret[-6:]})")

with open(tok_file, encoding='utf-8-sig') as f: tok = json.load(f)
tok["client_secret"] = new_secret
with open(tok_file, "w", encoding='utf-8') as f: json.dump(tok, f, indent=2)
print(f"  token.json updated")
'@
$tempUpdate = Join-Path $env:TEMP "google_update.py"
Set-Content -Path $tempUpdate -Value $updateScript -Encoding UTF8

try {
    & $Py $tempUpdate $CredFile $TokFile $tempSecret
    if ($LASTEXITCODE -ne 0) { throw "file update failed" }
} finally {
    Remove-Item -Path $tempUpdate -ErrorAction SilentlyContinue
    Remove-Item -Path $tempSecret -ErrorAction SilentlyContinue
}

# 3. WCM mirror (audit + recovery if files get nuked)
Write-Host ""
Write-Host "Mirroring in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','google_oauth_client_secret','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM mirror failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/google_oauth_client_secret" -ForegroundColor Green

# 4. Restart google-workspace-mcp
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting google-workspace-mcp..." -ForegroundColor Cyan
    docker compose --env-file (Join-Path $RepoRoot ".env.local") `
        -f (Join-Path $RepoRoot "docker-compose.yml") `
        up -d --no-deps --force-recreate google-workspace-mcp 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $prev
}

Write-Host ""
Start-Sleep -Seconds 3
Write-Host -NoNewline "google-workspace-mcp loopback: "
curl.exe -s -m 5 -o $null -w "HTTP=%{http_code}`n" http://127.0.0.1:8089/health

Write-Host ""
Write-Host "[ok] Done. Old client_secret can be deleted in Google Cloud Console once you confirm a real call works." -ForegroundColor Green
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue
