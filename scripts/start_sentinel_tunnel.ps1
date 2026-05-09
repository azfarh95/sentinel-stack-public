# start_sentinel_tunnel.ps1
# Starts a cloudflared quick tunnel -> sentinel_bridge port 8097.
# Captures the trycloudflare.com URL, writes it to sentinel_config.json,
# then fires keyboard_bot.py to refresh the pinned dashboard panel.

$BridgePort = 8097
$ConfigFile = "C:\Users\azfar\metamcp-local\sentinel_config.json"
$LogFile    = "$env:TEMP\sentinel_tunnel.log"
$PidFile    = "$env:TEMP\sentinel_tunnel.pid"

$cfExe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cfExe)) {
    $found = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($found) { $cfExe = $found.Source }
}
if (-not (Test-Path $cfExe)) {
    Write-Host "   ERROR: cloudflared not found"
    exit 1
}

# Kill any existing quick tunnel process
if (Test-Path $PidFile) {
    $oldPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($oldPid) {
        Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
        Write-Host "   Stopped old tunnel (pid=$oldPid)"
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# Clear old log
Set-Content $LogFile "" -Encoding utf8

# Start tunnel in background, redirect stderr (URL appears there) to log file
Write-Host "   Starting quick tunnel -> http://localhost:$BridgePort ..."
$proc = Start-Process -FilePath $cfExe `
    -ArgumentList "tunnel", "--url", "http://localhost:$BridgePort" `
    -RedirectStandardError $LogFile `
    -PassThru -WindowStyle Hidden

Set-Content $PidFile $proc.Id -Encoding ascii
Write-Host "   Tunnel process started (pid=$($proc.Id))"

# Wait for the URL to appear in the log (up to 30s)
$url = ""
$deadline = (Get-Date).AddSeconds(30)
while (-not $url -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $lines = Get-Content $LogFile -ErrorAction SilentlyContinue
    foreach ($line in $lines) {
        if ($line -match "https://[a-z0-9\-]+\.trycloudflare\.com") {
            $url = $Matches[0]
            break
        }
    }
}

if (-not $url) {
    Write-Host "   WARN: Could not capture tunnel URL within 30s"
    Write-Host "   Check $LogFile for details"
    exit 1
}

Write-Host "   Tunnel URL: $url"

# Update sentinel_config.json
$cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$cfg.mini_app_url = $url
$json = $cfg | ConvertTo-Json
[System.IO.File]::WriteAllText($ConfigFile, $json, [System.Text.UTF8Encoding]::new($false))
Write-Host "   Updated sentinel_config.json"

# Re-run keyboard_bot to refresh the pinned panel
Write-Host "   Refreshing Telegram panel..."
py -3 "C:\Users\azfar\metamcp-local\scripts\keyboard_bot.py"

Write-Host "   Done - dashboard available at $url"
