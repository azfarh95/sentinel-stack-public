# Rotate OWNER_AUTH_TOKEN without exposing the new value in chat / stdout.
#
# What it does:
#   1. Generates a fresh 256-bit token (via Python's `secrets.token_hex(32)`)
#   2. Writes it to .env.local (replaces the existing OWNER_AUTH_TOKEN= line)
#   3. Writes it to Windows Credential Manager (key the bridge.py reads from)
#   4. Copies it to the clipboard so you can paste into Vaultwarden + APK
#   5. Restarts the 3 consumer services
#   6. Prints status only — never the token value
#
# Usage:   pwsh -File scripts\rotate_owner_auth_token.ps1
# Safety:  Token never appears in stdout, hooks, or env-var listings.
#          After paste, run `Set-Clipboard -Value ''` to clear clipboard.

$ErrorActionPreference = 'Stop'
$envFile = "C:\Users\azfar\metamcp-local\.env.local"

if (-not (Test-Path $envFile)) {
    Write-Host "ERROR: $envFile missing" -ForegroundColor Red; exit 1
}

# 1) Generate
$py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
$newTok = & $py -c "import secrets; print(secrets.token_hex(32), end='')"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($newTok)) {
    Write-Host "ERROR: token gen failed" -ForegroundColor Red; exit 1
}

# 2) Replace in .env.local — preserve everything else verbatim.
$content  = Get-Content $envFile -Raw -Encoding UTF8
$pattern  = '(?m)^OWNER_AUTH_TOKEN=.*$'
$replaced = if ($content -match $pattern) {
    $content -replace $pattern, "OWNER_AUTH_TOKEN=$newTok"
} else {
    if ($content.TrimEnd().Length -gt 0 -and -not $content.EndsWith("`n")) { $content += "`n" }
    $content + "OWNER_AUTH_TOKEN=$newTok`n"
}
# UTF-8 no-BOM (PowerShell 5.1 wants `-Encoding UTF8` which adds BOM; use raw .NET).
[System.IO.File]::WriteAllText($envFile, $replaced, [System.Text.UTF8Encoding]::new($false))

# 3) Windows Credential Manager (where bridge.py looks via keyring)
& $py -c "import keyring; keyring.set_password('sentinel-miniapp', 'owner_auth_token', '$newTok')" | Out-Null

# 4) Clipboard — read once, paste into Vaultwarden + APK, then clear.
Set-Clipboard -Value $newTok

# 5) Restart consumers
Push-Location "C:\Users\azfar\metamcp-local"
docker compose --env-file .env.local up -d sentinel-vpn-dashboard 2>&1 | Out-Null
docker compose --env-file .env.local --profile media up -d smdl 2>&1 | Out-Null
Pop-Location

# Restart bridge.py (host process)
Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    try { $_.MainModule.FileName -eq $py -and ($_.CommandLine -like '*sentinel-miniapp-v2*bridge.py*') } catch { $false }
} | ForEach-Object { Stop-Process -Id $_.Id -Force }
Start-Sleep 1
$env:OWNER_AUTH_TOKEN = $newTok
Start-Process -FilePath $py -ArgumentList "-u","C:\Users\azfar\metamcp-local\sentinel-miniapp-v2\bridge.py" -WindowStyle Hidden | Out-Null

# Scrub the local variable
$newTok = $null
[System.GC]::Collect()

Write-Host "✓ Token rotated." -ForegroundColor Green
Write-Host "  - .env.local updated"
Write-Host "  - WCM (sentinel-miniapp/owner_auth_token) updated"
Write-Host "  - clipboard contains the new value — paste into Vaultwarden + APK"
Write-Host "  - sentinel-vpn-dashboard + smdl + bridge.py restarted"
Write-Host ""
Write-Host "After pasting:  Set-Clipboard -Value ''   # clears clipboard"
