#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers Windows Task Scheduler tasks for AI stack backups.
    Run once from an elevated PowerShell prompt.
    Tasks run as the current interactive user (no stored password required).
#>

$scriptDir = $PSScriptRoot

# ── Lean: daily at 02:00 ──────────────────────────────────────────────────────
$leanAction  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptDir\BACKUP_LEAN.ps1`""
$leanTrigger = New-ScheduledTaskTrigger -Daily -At "02:00"
$settings    = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew `
    -WakeToRun:$false
$principal   = New-ScheduledTaskPrincipal -UserId "azfar" -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName "AIStack-Backup-Lean" `
    -Action $leanAction -Trigger $leanTrigger -Settings $settings -Principal $principal `
    -Description "Daily lean backup of AI stack (~73 MB). Destination via SENTINEL_BACKUP_ROOT env var or default $env:USERPROFILE\Sentinel-Backups\lean\" `
    -Force | Select-Object TaskName, State

# ── Full: weekly Sunday at 03:00 ─────────────────────────────────────────────
$fullAction  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptDir\BACKUP_FULL.ps1`""
$fullTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At "03:00"
$fullSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew `
    -WakeToRun:$false

Register-ScheduledTask -TaskName "AIStack-Backup-Full" `
    -Action $fullAction -Trigger $fullTrigger -Settings $fullSettings -Principal $principal `
    -Description "Weekly full backup of AI stack (~2.1 GB). Destination via SENTINEL_BACKUP_ROOT env var or default $env:USERPROFILE\Sentinel-Backups\full\" `
    -Force | Select-Object TaskName, State

Write-Host ""
Write-Host "Tasks registered. Verify with:"
Write-Host '  Get-ScheduledTask -TaskName "AIStack-Backup-Lean","AIStack-Backup-Full" | Select TaskName,State'
