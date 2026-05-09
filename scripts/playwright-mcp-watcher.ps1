# Playwright MCP Watcher
# Starts Playwright MCP + IPv4 proxy when LM Studio is running, stops both when LM Studio exits.

$LMStudioProcess = "LM Studio"
$PlaywrightPort  = 8931
$ProxyPort       = 8932
$NpxPath         = "C:\Program Files\nodejs\npx.cmd"
$NodePath        = "C:\Program Files\nodejs\node.exe"
$ProxyScript     = "C:\Users\azfar\metamcp-local\scripts\playwright-mcp-proxy.js"
$LogFile         = "C:\Users\azfar\metamcp-local\scripts\playwright-mcp-watcher.log"

function Write-Log {
    param($msg)
    Add-Content -Path $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
}

function Get-PidOnPort {
    param($port)
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) { return $conn.OwningProcess }
    return $null
}

function Start-PlaywrightMCP {
    if (Get-PidOnPort $PlaywrightPort) {
        Write-Log "Playwright already on port $PlaywrightPort - skipping."
    } else {
        Write-Log "Starting Playwright MCP on port $PlaywrightPort."
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$NpxPath`" -y @playwright/mcp@latest --port $PlaywrightPort --cdp-endpoint http://127.0.0.1:9222 --allowed-hosts * --shared-browser-context" -WindowStyle Hidden
        Start-Sleep -Seconds 8
    }

    if (Get-PidOnPort $ProxyPort) {
        Write-Log "Proxy already on port $ProxyPort - skipping."
    } else {
        Write-Log "Starting IPv4 proxy on port $ProxyPort."
        Start-Process -FilePath $NodePath -ArgumentList $ProxyScript -WindowStyle Hidden
    }
}

function Stop-PlaywrightMCP {
    $ppid = Get-PidOnPort $PlaywrightPort
    if ($ppid) {
        Write-Log "Stopping Playwright MCP (PID $ppid)."
        Stop-Process -Id $ppid -Force -ErrorAction SilentlyContinue
    }
    $rpid = Get-PidOnPort $ProxyPort
    if ($rpid) {
        Write-Log "Stopping proxy (PID $rpid)."
        Stop-Process -Id $rpid -Force -ErrorAction SilentlyContinue
    }
    if (-not $ppid -and -not $rpid) {
        Write-Log "LM Studio closed - Playwright was not running."
    }
}

Write-Log "Watcher started."
$wasRunning = $false

while ($true) {
    $lmRunning = [bool](Get-Process -Name $LMStudioProcess -ErrorAction SilentlyContinue)

    if ($lmRunning -and -not $wasRunning) {
        Start-PlaywrightMCP
        $wasRunning = $true
    } elseif (-not $lmRunning -and $wasRunning) {
        Stop-PlaywrightMCP
        $wasRunning = $false
    }

    Start-Sleep -Seconds 15
}
