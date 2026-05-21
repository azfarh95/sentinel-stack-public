# End-to-end diagnostic for the Comet→Playwright→OpenClaw chain.
# Verifies each link and prints a single-line summary per check.

[CmdletBinding()]
param([int]$CdpPort = 9222, [int]$PwPort = 8931, [int]$ProxyPort = 8932, [int]$BridgePort = 8101)

function Check($name, [scriptblock]$probe) {
    Write-Host -NoNewline ("{0,-32}" -f $name)
    try {
        $r = & $probe
        if ($r) { Write-Host "OK   " -ForegroundColor Green -NoNewline; Write-Host $r }
        else    { Write-Host "FAIL " -ForegroundColor Red }
    } catch { Write-Host "FAIL " -ForegroundColor Red -NoNewline; Write-Host $_.Exception.Message }
}

Write-Host ""
Write-Host "Comet ↔ Playwright ↔ OpenClaw chain diagnostic" -ForegroundColor Cyan
Write-Host "----------------------------------------------" -ForegroundColor Cyan

Check "Comet running" {
    $p = Get-CimInstance Win32_Process -Filter "Name='comet.exe'" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    "$($p.Count) processes"
}

Check "Comet has --remote-debug flag" {
    $p = Get-CimInstance Win32_Process -Filter "Name='comet.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -match "--remote-debugging-port=$CdpPort" } |
         Select-Object -First 1
    if (-not $p) { return $false }
    "PID $($p.ProcessId)"
}

Check "CDP /json/version :$CdpPort" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$CdpPort/json/version" -UseBasicParsing -TimeoutSec 3
    $j = $r.Content | ConvertFrom-Json
    $j.Browser
}

Check "Playwright MCP :$PwPort" {
    $conn = Get-NetTCPConnection -LocalPort $PwPort -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) { return $false }
    "PID $($conn.OwningProcess)"
}

Check "Playwright IPv4 proxy :$ProxyPort" {
    $conn = Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) { return $false }
    "PID $($conn.OwningProcess)"
}

Check "Sidepanel bridge :$BridgePort /health" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$BridgePort/health" -UseBasicParsing -TimeoutSec 3
    ($r.Content | ConvertFrom-Json).uptime_s.ToString() + "s uptime"
}

Check "MetaMCP :12008" {
    # Docker Desktop's vpnkit port forwards don't show in Get-NetTCPConnection,
    # so use a raw TCP connect to verify reachability.
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect("127.0.0.1", 12008, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(1500)) { $c.EndConnect($iar); "reachable" } else { $false }
    } catch { $false } finally { $c.Close() }
}

Check "OpenClaw gateway WSL :18789" {
    $out = wsl -d Ubuntu-24.04 -u root -- bash -lc "ss -tln 2>/dev/null | grep -c ':18789'" 2>$null
    if ($out -and [int]$out -gt 0) { "listening" } else { $false }
}

Write-Host ""
