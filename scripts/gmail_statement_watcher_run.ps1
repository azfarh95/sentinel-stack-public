# gmail_statement_watcher_run.ps1
# Drives the bank-statement Gmail ingestor (Sentinel Finance task #1).
# Runs `python -m app.gmail_statement_watcher` INSIDE the portfolio-mcp
# container, which scans Gmail for PDFs from configured bank/broker/
# moneylender senders and drops them into the pipeline _INBOX. Then
# kicks `app.inbox_pipeline --apply --post` to classify + parse + journal.
#
# Idempotent: msg_id is logged in gmail_statement_ingest_log AND the
# Gmail label "Statement-ingested" is added on successful download, so
# overlapping runs are safe.
#
# Registered as Windows Scheduled Task "Sentinel Finance Statement Watcher".

$ErrorActionPreference = "Continue"
$LogFile = "$env:LOCALAPPDATA\gmail_statement_watcher.log"

function Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Log "=== Run started ==="

$running = (docker ps --filter "name=portfolio-mcp" --filter "status=running" --format "{{.Names}}") 2>$null
if (-not $running) { Log "portfolio-mcp not running - skip tick"; Log "=== Run finished ==="; exit 0 }

# 1) Pull new statements from Gmail.
$out = docker exec portfolio-mcp python -m app.gmail_statement_watcher 2>&1
$out | ForEach-Object { Log $_ }

# 2) Classify + parse + journal anything new in _INBOX.
# inbox_pipeline is already idempotent (external_id, doc-classifier dedupe)
# so re-running is safe even when the watcher pulled nothing.
$out2 = docker exec portfolio-mcp python -m app.inbox_pipeline --apply --post 2>&1
$out2 | ForEach-Object { Log $_ }

Log "=== Run finished ==="
exit 0
