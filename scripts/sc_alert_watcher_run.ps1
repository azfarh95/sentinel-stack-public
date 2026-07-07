# sc_alert_watcher_run.ps1
# Drives the SC card-alert ingestor (Sentinel Finance, task #26/#27).
# Runs `python -m app.sc_alert_watcher` INSIDE the portfolio-mcp container,
# which scans Gmail (alerts.sg@sc.com) and posts PROVISIONAL L0b journals.
# Idempotent: external_id + the SC-alert-ingested Gmail label prevent
# double-posting, so a 5-minute Scheduled Task with overlap is safe.
#
# Registered as Windows Scheduled Task "Sentinel SC Alert Watcher".
# Replaces the retired POSB->Firefly importer ("Firefly Auto Import").

$ErrorActionPreference = "Continue"
$LogFile = "$env:LOCALAPPDATA\sc_alert_watcher.log"

function Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Log "=== Run started ==="

# Container must be healthy; if it's down the daemon will bring it back -
# skip this tick rather than erroring.
$running = (docker ps --filter "name=portfolio-mcp" --filter "status=running" --format "{{.Names}}") 2>$null
if (-not $running) { Log "portfolio-mcp not running - skip tick"; Log "=== Run finished ==="; exit 0 }

$out = docker exec portfolio-mcp python -m app.sc_alert_watcher 2>&1
$out | ForEach-Object { Log $_ }

Log "=== Run finished ==="
exit 0
