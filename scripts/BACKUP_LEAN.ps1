#Requires -Version 5.1
<#
.SYNOPSIS
    Daily lean backup of the AI stack — excludes plugin-runtime-deps (~73 MB).
.PARAMETER BackupRoot
    Where to write backups. Defaults to $env:SENTINEL_BACKUP_ROOT, then to
    G:\AIStack-Backup (legacy owner setup), then to %USERPROFILE%\Sentinel-Backups.
.NOTES
    Scheduled: daily at 02:00 via Windows Task Scheduler task "AIStack-Backup-Lean"
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

$date   = Get-Date -Format "yyyy-MM-dd"
$dest   = Join-Path $BackupRoot "lean\$date"
$log    = Join-Path $BackupRoot "lean\backup.log"
$wslBase = "\\wsl$\Ubuntu-24.04\home\$env:USERNAME\.openclaw"
if (-not (Test-Path $wslBase)) {
    # Fallback for owner setup where WSL user differs from Windows user
    $wslBase = "\\wsl$\Ubuntu-24.04\home\azfar\.openclaw"
}

# Ensure destination exists
New-Item -ItemType Directory -Force -Path $dest | Out-Null

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line
}

Log "=== Lean backup started → $dest ==="

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
Copy-Item "$wslBase\credentials"  "$dest\wsl-credentials"  -Recurse -Force
Copy-Item "$wslBase\memory"       "$dest\wsl-memory"       -Recurse -Force
Copy-Item "$wslBase\agents"       "$dest\wsl-agents"       -Recurse -Force
Copy-Item "$wslBase\workspace"    "$dest\wsl-workspace"    -Recurse -Force
Copy-Item "$wslBase\tasks"        "$dest\wsl-tasks"        -Recurse -Force
Copy-Item "$wslBase\completions"  "$dest\wsl-completions"  -Recurse -Force
Copy-Item "$wslBase\media"        "$dest\wsl-media"        -Recurse -Force
Copy-Item "\\wsl$\Ubuntu-24.04\etc\systemd\system\openclaw-gateway.service" $dest -Force
Copy-Item "\\wsl$\Ubuntu-24.04\etc\wsl.conf"                                $dest -Force

# ── Summary ───────────────────────────────────────────────────────────────────
$size = (Get-ChildItem $dest -Recurse -File | Measure-Object -Property Length -Sum).Sum
Log ("Lean backup complete. Size: {0:N1} MB" -f ($size / 1MB))

# Prune backups older than 14 days
$cutoff = (Get-Date).AddDays(-14)
Get-ChildItem (Join-Path $BackupRoot "lean") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' -and [datetime]$_.Name -lt $cutoff } |
    ForEach-Object {
        Log "Pruning old backup: $($_.FullName)"
        Remove-Item $_.FullName -Recurse -Force
    }
