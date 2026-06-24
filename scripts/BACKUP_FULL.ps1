#Requires -Version 5.1
<#
.SYNOPSIS
    Weekly full backup of the AI stack — includes plugin-runtime-deps (~2.1 GB).
.PARAMETER BackupRoot
    Where to write backups. Defaults to $env:SENTINEL_BACKUP_ROOT, then to
    G:\AIStack-Backup (legacy owner setup), then to %USERPROFILE%\Sentinel-Backups.
.NOTES
    Scheduled: weekly on Sunday at 03:00 via Windows Task Scheduler task "AIStack-Backup-Full"
#>

param(
    [string]$BackupRoot
)

if (-not $BackupRoot) {
    if ($env:SENTINEL_BACKUP_ROOT) {
        $BackupRoot = $env:SENTINEL_BACKUP_ROOT
    } elseif (Test-Path "G:\AIStack-Backup") {
        $BackupRoot = "G:\AIStack-Backup"
    } else {
        $BackupRoot = Join-Path $env:USERPROFILE "Sentinel-Backups"
    }
}

$date    = Get-Date -Format "yyyy-MM-dd"
$dest    = Join-Path $BackupRoot "full\$date"
$log     = Join-Path $BackupRoot "full\backup.log"
$wslBase = "\\wsl$\Ubuntu-24.04\home\$env:USERNAME\.openclaw"
if (-not (Test-Path $wslBase)) {
    $wslBase = "\\wsl$\Ubuntu-24.04\home\azfar\.openclaw"
}

New-Item -ItemType Directory -Force -Path $dest | Out-Null

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line
}

Log "=== Full backup started → $dest ==="

# ── Windows-side ──────────────────────────────────────────────────────────────
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$UserHome = $env:USERPROFILE
Log "Copying Windows-side files..."
Copy-Item (Join-Path $UserHome ".wslconfig")               $dest -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $UserHome ".openclaw") "$dest\win-openclaw" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $RepoRoot "scripts")     "$dest\scripts" -Recurse -Force
Copy-Item (Join-Path $RepoRoot "docker-compose.local.yml") $dest -Force
Copy-Item (Join-Path $RepoRoot "Maintenance") "$dest\Maintenance" -Recurse -Force -ErrorAction SilentlyContinue

# ── WSL-side (via UNC path) ───────────────────────────────────────────────────
Log "Copying WSL-side files..."
Copy-Item "$wslBase\openclaw.json"           $dest -Force
Copy-Item "$wslBase\openclaw.json.last-good" $dest -Force
Copy-Item "$wslBase\credentials"         "$dest\wsl-credentials"         -Recurse -Force
Copy-Item "$wslBase\memory"              "$dest\wsl-memory"              -Recurse -Force
Copy-Item "$wslBase\agents"              "$dest\wsl-agents"              -Recurse -Force
Copy-Item "$wslBase\workspace"           "$dest\wsl-workspace"           -Recurse -Force
Copy-Item "$wslBase\tasks"               "$dest\wsl-tasks"               -Recurse -Force
Copy-Item "$wslBase\completions"         "$dest\wsl-completions"         -Recurse -Force
Copy-Item "$wslBase\media"               "$dest\wsl-media"               -Recurse -Force
# Use WSL tar to avoid Windows MAX_PATH limits on deep npm cache paths inside plugin-runtime-deps
$tarDest = "$dest\wsl-plugin-runtime-deps.tar.gz"
Log "Archiving plugin-runtime-deps via WSL tar (avoids MAX_PATH limits)..."
$wslUser = if ($env:SENTINEL_WSL_USER) { $env:SENTINEL_WSL_USER } else { $env:USERNAME }
wsl -d Ubuntu-24.04 -u $wslUser -- bash -c "tar czf /tmp/openclaw-plugin-deps-backup.tar.gz -C `$HOME/.openclaw plugin-runtime-deps"
Copy-Item "\\wsl$\Ubuntu-24.04\tmp\openclaw-plugin-deps-backup.tar.gz" $tarDest -Force
wsl -d Ubuntu-24.04 -u root -- rm -f /tmp/openclaw-plugin-deps-backup.tar.gz
Copy-Item "\\wsl$\Ubuntu-24.04\etc\systemd\system\openclaw-gateway.service" $dest -Force
Copy-Item "\\wsl$\Ubuntu-24.04\etc\wsl.conf"                                $dest -Force

# ── Summary ───────────────────────────────────────────────────────────────────
$size = (Get-ChildItem $dest -Recurse -File | Measure-Object -Property Length -Sum).Sum
Log ("Full backup complete. Size: {0:N1} MB" -f ($size / 1MB))

# Prune backups older than 60 days (keep ~8 weekly snapshots)
$cutoff = (Get-Date).AddDays(-60)
Get-ChildItem (Join-Path $BackupRoot "full") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' -and [datetime]$_.Name -lt $cutoff } |
    ForEach-Object {
        Log "Pruning old backup: $($_.FullName)"
        Remove-Item $_.FullName -Recurse -Force
    }
