# Launch Comet with --remote-debugging-port=9222 so Playwright MCP can attach.
# Use this instead of the regular Comet shortcut when you want OpenClaw to drive
# the browser via the sidepanel extension.
#
# Behavior:
#   - If Comet is already running WITH the flag → bring window to front.
#   - If Comet is already running WITHOUT the flag → prompt to close & relaunch.
#   - If Comet is not running → launch with the flag.
#   - Verifies 127.0.0.1:9222 is up before returning.

[CmdletBinding()]
param(
    [int]$Port = 9222,
    [switch]$Force,        # close existing Comet without prompting
    [switch]$Quiet         # suppress info output; still prints warnings
)

$ErrorActionPreference = "Stop"
$CometExe = "$env:LOCALAPPDATA\Perplexity\Comet\Application\comet.exe"
$Flag     = "--remote-debugging-port=$Port"

function Say($msg, $col = "White") {
    if (-not $Quiet) { Write-Host $msg -ForegroundColor $col }
}

function Get-CometProcesses {
    Get-CimInstance Win32_Process -Filter "Name='comet.exe'" -ErrorAction SilentlyContinue
}

function Test-CdpUp {
    param([int]$P)
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$P/json/version" -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

if (-not (Test-Path $CometExe)) {
    Write-Error "Comet executable not found at $CometExe"
    exit 2
}

$procs = @(Get-CometProcesses)
$running = $procs.Count -gt 0
$flaggedProc = $procs | Where-Object { $_.CommandLine -and ($_.CommandLine -match [regex]::Escape($Flag)) } | Select-Object -First 1

if ($flaggedProc) {
    Say "Comet already running with $Flag (PID $($flaggedProc.ProcessId))." Green
    if (Test-CdpUp $Port) {
        Say "CDP endpoint http://127.0.0.1:$Port responding." Green
        exit 0
    } else {
        Say "WARN: Comet has the flag but CDP not responding on $Port — restart may help." Yellow
        exit 1
    }
}

if ($running) {
    # Comet is running WITHOUT the flag
    if (-not $Force) {
        Say "Comet is running without the debug flag." Yellow
        Say "Press Enter to close all $($procs.Count) Comet processes and relaunch with $Flag, or Ctrl+C to abort." Yellow
        $null = Read-Host
    }
    Say "Closing $($procs.Count) Comet process(es)…" Cyan
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
    # Wait up to 8s for Comet to fully exit (so user-data-dir lock is released).
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline -and (Get-CometProcesses).Count -gt 0) {
        Start-Sleep -Milliseconds 300
    }
    if ((Get-CometProcesses).Count -gt 0) {
        Say "WARN: some Comet processes did not exit cleanly. Try Task Manager." Red
    }
}

Say "Launching Comet with $Flag…" Cyan
$args = @($Flag, "--remote-debugging-address=127.0.0.1")
Start-Process -FilePath $CometExe -ArgumentList $args | Out-Null

# Poll for CDP up to 15s
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    if (Test-CdpUp $Port) {
        Say "CDP endpoint http://127.0.0.1:$Port live — Playwright MCP can attach." Green
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
Say "WARN: Comet launched but CDP not responding on $Port after 15s." Yellow
Say "      Try opening any non-extension page in Comet and re-check /json/version." Yellow
exit 1
