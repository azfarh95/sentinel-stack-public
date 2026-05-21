# scripts/rotate_watchdog_bot.ps1
#
# Rotates the @YourWatchdogBot token (the watchdog bot
# consumed by watchdog.py running as native Windows pythonw process).
#
# Pre-req: /revoke this bot in BotFather first, then have the new token
#          ready to paste at the hidden prompt.
#
# Usage:  .\scripts\rotate_watchdog_bot.ps1

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Py)) { $Py = (Get-Command py).Source }

Write-Host ""
Write-Host "── Rotate YourWatchdogBot (watchdog) token ──" -ForegroundColor Cyan
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

# 3. Update WCM
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-watchdog','bot_token','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  ✓ WCM updated: sentinel-watchdog/bot_token" -ForegroundColor Green

# 4. Restart watchdog.py — kill the pythonw process, relaunch detached
Write-Host ""
Write-Host "Restarting watchdog.py..." -ForegroundColor Cyan
$wd = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'watchdog\\watchdog\.py' } | Select-Object -First 1
if ($wd) {
    Write-Host "  killing watchdog PID $($wd.ProcessId)" -ForegroundColor Gray
    Stop-Process -Id $wd.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
$pythonw = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\pythonw.exe"
$script  = "C:\Users\azfar\metamcp-local\watchdog\watchdog.py"
Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" `
              -WorkingDirectory "C:\Users\azfar\metamcp-local\watchdog" `
              -WindowStyle Hidden
Start-Sleep -Seconds 3

$newPid = (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'watchdog\\watchdog\.py' } | Select-Object -First 1).ProcessId
if ($newPid) {
    Write-Host "  ✓ watchdog.py relaunched (PID $newPid)" -ForegroundColor Green
} else {
    Write-Host "  ⚠ watchdog.py PID not visible — check manually" -ForegroundColor Yellow
}

# 5. Verify the new token is alive
Write-Host ""
Write-Host "── Verification ──" -ForegroundColor Cyan
$verify = (curl.exe -s "https://api.telegram.org/bot$tok/getMe") | ConvertFrom-Json
Write-Host "  username : @$($verify.result.username)"
Write-Host "  name     : $($verify.result.first_name)"
Write-Host "  bot ID   : $($verify.result.id)"

# Wipe local
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "✓ Done. Test by sending @YourWatchdogBot a /status command." -ForegroundColor Green
