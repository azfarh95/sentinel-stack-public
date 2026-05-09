# Registers the Sentinel Watchdog as a Windows Task Scheduler task.

$taskName    = "Sentinel Watchdog"
$watchdogDir = $PSScriptRoot
$scriptPath  = Join-Path $watchdogDir "watchdog.py"

if (-not (Test-Path $scriptPath)) {
    Write-Error "watchdog.py not found at $scriptPath"
    exit 1
}

# Resolve Python from the py launcher
$pyExe     = (Get-Command py -ErrorAction Stop).Source
$pythonDir = & py -c "import sys, os; print(os.path.dirname(sys.executable))"
$pythonwExe = Join-Path $pythonDir "pythonw.exe"
if (-not (Test-Path $pythonwExe)) {
    # Fall back to python.exe (shows a console window but still works)
    $pythonwExe = Join-Path $pythonDir "python.exe"
}

# Install dependencies
Write-Host "Installing dependencies..."
& py -m pip install -r (Join-Path $watchdogDir "requirements.txt") -q
if ($LASTEXITCODE -ne 0) { Write-Warning "pip install had errors — check manually" }

$action = New-ScheduledTaskAction `
    -Execute $pythonwExe `
    -Argument "`"$scriptPath`"" `
    -WorkingDirectory $watchdogDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

if ($LASTEXITCODE -ne 0 -and -not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    Write-Error "Task registration failed."
    exit 1
}

Write-Host ""
Write-Host "Registered: '$taskName'"
Write-Host "  Script : $scriptPath"
Write-Host "  Python : $pythonwExe"
Write-Host "  Trigger: at logon (auto-restart on crash)"
Write-Host ""
Write-Host "Starting now..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2
$state = (Get-ScheduledTask -TaskName $taskName).State
Write-Host "Task state: $state"
Write-Host "Done."
