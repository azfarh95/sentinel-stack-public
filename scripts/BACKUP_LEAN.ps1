#Requires -Version 5.1
<#
.SYNOPSIS
    Daily lean backup of the AI stack — excludes plugin-runtime-deps (~73 MB).
.NOTES
    Destination: G:\AIStack-Backup\lean\YYYY-MM-DD\
    Scheduled: daily at 02:00 via Windows Task Scheduler task "AIStack-Backup-Lean"
#>

$date   = Get-Date -Format "yyyy-MM-dd"
$dest   = "G:\AIStack-Backup\lean\$date"
$log    = "G:\AIStack-Backup\lean\backup.log"
$wslBase = "\\wsl$\Ubuntu-24.04\home\azfar\.openclaw"

# Ensure destination exists
New-Item -ItemType Directory -Force -Path $dest | Out-Null

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line
}

Log "=== Lean backup started → $dest ==="

# ── Windows-side ──────────────────────────────────────────────────────────────
Log "Copying Windows-side files..."
Copy-Item "C:\Users\azfar\.wslconfig"                         $dest -Force
Copy-Item "C:\Users\azfar\.openclaw"        "$dest\win-openclaw"  -Recurse -Force
Copy-Item "C:\Users\azfar\metamcp-local\scripts"  "$dest\scripts" -Recurse -Force
Copy-Item "C:\Users\azfar\metamcp-local\docker-compose.local.yml" $dest -Force
Copy-Item "C:\Users\azfar\metamcp-local\Maintenance" "$dest\Maintenance" -Recurse -Force

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
Get-ChildItem "G:\AIStack-Backup\lean" -Directory |
    Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' -and [datetime]$_.Name -lt $cutoff } |
    ForEach-Object {
        Log "Pruning old backup: $($_.FullName)"
        Remove-Item $_.FullName -Recurse -Force
    }
