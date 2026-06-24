<#
.SYNOPSIS
  Register the SMDL redeploy as an elevated scheduled task — run ONCE, elevated.

.DESCRIPTION
  Creates the on-demand task "SentinelSMDLRedeploy" that runs
  scripts\smdl-redeploy.ps1 with Highest privileges as the current user (so it
  has both Docker Desktop access AND the elevation tailscale serve needs).

  After this one elevated registration, you can redeploy smdl with NO elevation
  prompt, from any shell:

      schtasks /run /tn SentinelSMDLRedeploy        # or: Start-ScheduledTask SentinelSMDLRedeploy

  Run THIS script from an elevated PowerShell:
      pwsh -File scripts\register-smdl-redeploy-task.ps1
#>
[CmdletBinding()]
param(
  [string]$StackDir = 'C:\Users\azfar\metamcp-local',
  [string]$TaskName = 'SentinelSMDLRedeploy'
)
$ErrorActionPreference = 'Stop'

$script = Join-Path $StackDir 'scripts\smdl-redeploy.ps1'
if (-not (Test-Path $script)) { throw "redeploy script not found: $script" }

$pwsh = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $pwsh) { $pwsh = (Get-Command powershell.exe).Source }

$action = New-ScheduledTaskAction -Execute $pwsh `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
  -WorkingDirectory $StackDir
# Run as the current user, elevated (Highest) — gives Docker Desktop access
# (user session) plus the privilege tailscale serve needs. On-demand only:
# no time/logon trigger.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
  -Settings $settings -Description 'On-demand: serve-off -> recreate smdl -> serve-on (the :8096 dance).' -Force | Out-Null

Write-Host "[register] task '$TaskName' registered. Trigger anytime (no elevation) with:"
Write-Host "    schtasks /run /tn $TaskName"
