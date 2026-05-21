# firefly_auto_import.ps1
# Daily auto-import for POSB bank statements.
# 1. Check if any PDF in the watch dir is newer than the last-run marker.
# 2. If yes: convert PDFs → CSVs, then push CSVs to Firefly III via REST API.
# 3. Telegram-notify only when new transactions were actually created (ok > 0).
# Idempotent — re-running with no new PDFs is a no-op.
#
# Triggered by Windows Scheduled Task "Firefly Auto Import" (see registration below).

$ErrorActionPreference = "Stop"

$WatchDir   = "C:\Users\azfar\OneDrive\CC_Statement\Statements by bank\Bank Statements"
$ScriptDir  = "C:\Users\azfar\metamcp-local\scripts"
$LogFile    = "$env:LOCALAPPDATA\firefly_auto_import.log"
$MarkerFile = "$env:LOCALAPPDATA\firefly_auto_import.lastrun"
$Pythonw    = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe"
$Python     = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"

function Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Log "=== Run started ==="

# 1) Find latest PDF mtime in watch dir
if (-not (Test-Path $WatchDir)) { Log "WatchDir missing — abort"; exit 1 }
$pdfs = Get-ChildItem -Path $WatchDir -Filter "*.pdf" -ErrorAction SilentlyContinue
if (-not $pdfs) { Log "No PDFs in watch dir — abort"; exit 0 }
$latest = ($pdfs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
Log "Latest PDF mtime: $latest"

# 2) Compare to marker
$lastRun = $null
if (Test-Path $MarkerFile) {
    try { $lastRun = [datetime](Get-Content $MarkerFile -Raw).Trim() } catch { $lastRun = $null }
}
if ($lastRun -and $latest -le $lastRun) {
    Log "No new PDFs since last run ($lastRun) — exit"
    exit 0
}
Log "New/modified PDFs detected (last run: $lastRun) — proceed"

# 3) Run converter (PDF -> CSV) via Start-Process to avoid PS pipe quirks
$convOut = "$env:TEMP\firefly_convert.out"
$convErr = "$env:TEMP\firefly_convert.err"
Log "Step 1/2: converting PDFs to CSV"
$p = Start-Process -FilePath $Python -ArgumentList "$ScriptDir\posb_to_firefly_csv.py" `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $convOut -RedirectStandardError $convErr
if ($p.ExitCode -ne 0) {
    Log "Converter failed exit=$($p.ExitCode) — abort"
    Get-Content $convErr -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Log "  conv-err: $_" }
    exit 1
}
Log "Converter ok"

# 4) Run importer (CSV -> Firefly API)
$impOut = "$env:TEMP\firefly_import.out"
$impErr = "$env:TEMP\firefly_import.err"
Log "Step 2/2: importing CSV rows to Firefly III"
$p = Start-Process -FilePath $Python -ArgumentList "$ScriptDir\firefly_import_csv.py" `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $impOut -RedirectStandardError $impErr
if ($p.ExitCode -ne 0) {
    Log "Importer failed exit=$($p.ExitCode) — abort"
    Get-Content $impErr -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Log "  imp-err: $_" }
    exit 1
}

# 5) Parse Grand total line
$grand = (Get-Content $impOut | Select-String '^=== Grand total: ok=(\d+)\s+dup=(\d+)\s+err=(\d+) ===' | Select-Object -Last 1)
$ok = 0; $dup = 0; $err = 0
if ($grand) {
    $m = $grand.Matches[0]
    $ok = [int]$m.Groups[1].Value
    $dup = [int]$m.Groups[2].Value
    $err = [int]$m.Groups[3].Value
}
Log "Import summary: ok=$ok dup=$dup err=$err"

# 6) Update marker (even if 0 new, so we don't re-scan tomorrow)
Set-Content -Path $MarkerFile -Value ($latest.ToString("o")) -Encoding utf8

# 7) Telegram-notify only if any new rows OR any errors
if ($ok -gt 0 -or $err -gt 0) {
    $newest = $pdfs | Sort-Object LastWriteTime -Descending | Select-Object -First 3 | ForEach-Object { "- $($_.Name) ($($_.LastWriteTime.ToString('yyyy-MM-dd')))" }
    $newestStr = $newest -join "`n"
    $subject = if ($err -gt 0) { "Firefly auto-import: $ok new, $err ERROR" } else { "Firefly auto-import: $ok new transactions" }
    $body = "New: $ok`nDuplicates skipped: $dup`nErrors: $err`n`nRecent statements:`n$newestStr"
    try {
        & "$ScriptDir\notify_owner.ps1" -Subject $subject -Message $body | Out-Null
        Log "Telegram notify sent"
    } catch {
        Log "Telegram notify FAILED: $_"
    }
} else {
    Log "No new transactions — skipping notify"
}

Log "=== Run finished ==="
