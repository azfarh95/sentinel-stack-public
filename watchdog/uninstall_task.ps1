# Stops and removes the Sentinel Watchdog scheduled task.

$taskName = "Sentinel Watchdog"

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Task '$taskName' not found — nothing to remove."
    exit 0
}

Stop-ScheduledTask  -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false

Write-Host "Removed: '$taskName'"
