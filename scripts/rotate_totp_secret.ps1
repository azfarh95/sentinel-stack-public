# scripts/rotate_totp_secret.ps1
#
# Rotates the TOTP shared secret used by sentinel-miniapp-v2/bridge.py
# for the auth gate. Generates a new base32 seed via pyotp, stores it
# in WCM, restarts bridge.py (which auto-regenerates totp_setup.html
# with the new QR code), then opens that page so you can re-scan with
# your authenticator app.
#
# Side effect: the OLD secret is invalidated immediately. Any current
# Mini App session token issued with it stays valid until it expires,
# but no NEW logins will work until you scan the new QR.
#
# Pre-req: have your authenticator app (Google / Microsoft / Authy /
# 1Password / Bitwarden) ready on your phone. You'll delete the old
# "Sentinel" entry and scan a new one.
#
# Usage:  .\scripts\rotate_totp_secret.ps1

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
$BridgeDir  = "C:\Users\azfar\metamcp-local\sentinel-miniapp-v2"
$SetupHtml  = Join-Path $BridgeDir "totp_setup.html"

Write-Host ""
Write-Host "── Rotate TOTP secret (sentinel-miniapp-v2 auth gate) ──" -ForegroundColor Cyan
Write-Host "A new base32 seed will be generated locally, stored in WCM, and a fresh QR code rendered." -ForegroundColor Yellow
Write-Host ""

# 1. Generate new base32 secret
Write-Host "Generating new base32 secret..." -ForegroundColor Cyan
$newSecret = & $Py -c "import pyotp; print(pyotp.random_base32())"
if (-not $newSecret -or $newSecret.Length -lt 16) {
    Write-Host "[x] pyotp.random_base32() returned bad output: $newSecret" -ForegroundColor Red
    exit 1
}
$newSecret = $newSecret.Trim()
Write-Host "  [ok] $($newSecret.Length)-char secret generated (preview: ...$($newSecret.Substring($newSecret.Length-6)))" -ForegroundColor Green

# 2. Store in WCM
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','totp_secret','$newSecret')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/totp_secret" -ForegroundColor Green

# 3. Restart bridge.py - reads TOTP_SECRET from WCM at boot, regenerates totp_setup.html
Write-Host ""
Write-Host "Restarting bridge.py..." -ForegroundColor Cyan
$old = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'sentinel-miniapp-v2[\\/]bridge\.py'
} | Select-Object -First 1
if ($old) {
    Write-Host "  killing existing PID $($old.ProcessId)" -ForegroundColor Gray
    Stop-Process -Id $old.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Delete old setup page so we KNOW the new one is freshly generated (not stale)
Remove-Item -Path $SetupHtml -ErrorAction SilentlyContinue

$pythonw = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\pythonw.exe"
Start-Process -FilePath $pythonw -ArgumentList "`"$BridgeDir\bridge.py`"" `
              -WorkingDirectory $BridgeDir -WindowStyle Hidden
Start-Sleep -Seconds 5

$new = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'sentinel-miniapp-v2[\\/]bridge\.py'
} | Select-Object -First 1
if (-not $new) {
    Write-Host "[x] bridge.py failed to relaunch" -ForegroundColor Red
    exit 1
}
Write-Host "  [ok] bridge.py running PID $($new.ProcessId)" -ForegroundColor Green

# 4. Verify listener + new HTML
$listener = Get-NetTCPConnection -LocalPort 8098 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "  [ok] :8098 bound" -ForegroundColor Green
} else {
    Write-Host "  [warn] :8098 not yet listening - bridge may still be initializing" -ForegroundColor Yellow
}

if (-not (Test-Path $SetupHtml)) {
    Write-Host "[x] totp_setup.html NOT generated - bridge.py boot probably errored" -ForegroundColor Red
    exit 1
}
Write-Host "  [ok] totp_setup.html regenerated" -ForegroundColor Green

# 5. Open the setup page for QR scan
Write-Host ""
Write-Host "── ACTION REQUIRED ──" -ForegroundColor Cyan
Write-Host "Opening totp_setup.html in your default browser..." -ForegroundColor Yellow
Write-Host "  1. In your authenticator app: DELETE the existing 'Sentinel' entry"
Write-Host "  2. Add new account by scanning the QR (or paste the secret shown below it)"
Write-Host "  3. Test by logging into the Mini App via Telegram - new 6-digit code should work"
Start-Process $SetupHtml
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "[ok] Done. After confirming new authenticator entry works, delete totp_setup.html." -ForegroundColor Green
Write-Host "    (It contains the secret in plain text - filesystem-only but still worth removing.)" -ForegroundColor DarkGray

$newSecret = "x" * 64
Remove-Variable newSecret -ErrorAction SilentlyContinue
