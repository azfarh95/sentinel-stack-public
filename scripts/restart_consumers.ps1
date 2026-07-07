# scripts/restart_consumers.ps1
#
# Shared helper: given a logical secret name, look up its consumer list
# in scripts/secrets.yaml and bounce each one. Used by every rotate_*.ps1
# so that "which services care about this secret" lives in ONE place
# (the YAML), not duplicated across 12 rotation scripts.
#
# Background: 2026-05-11 watchdog kept falsely alerting "LM Studio API
# down" because rotate_lmstudio_api.ps1 restarted openclaw-gateway and
# infer-bridge but forgot the watchdog (which caches the key at boot).
# This helper enumerates from a single declarative map so additions
# automatically propagate.
#
# Consumer kinds handled:
#   docker         → docker restart <name>
#   wsl-systemd    → wsl systemctl restart <unit>
#   win-pythonw    → kill matching Win32_Process by CommandLine regex,
#                    relaunch via pythonw with detached window
#   win-service    → Restart-Service <name>
#   scheduled-task → Stop+Start ScheduledTask <name>
#
# If a consumer is marked `hot-reload: true` in secrets.yaml, skip the
# restart (the service re-reads the secret per-probe instead). Log it.
#
# Usage:
#   .\scripts\restart_consumers.ps1 -Secret lm_studio_api_key
#   .\scripts\restart_consumers.ps1 -Secret tavily_api_key -DryRun

param(
    [Parameter(Mandatory)] [string]$Secret,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
$PythonW = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\pythonw.exe"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$YamlFile = Join-Path $PSScriptRoot "secrets.yaml"

if (-not (Test-Path $YamlFile)) {
    Write-Host "[x] secrets.yaml not found at $YamlFile" -ForegroundColor Red
    exit 1
}

# Parse YAML via Python (PS doesn't have built-in YAML; using py + PyYAML
# which is already installed system-wide for the other scripts).
$consumersRaw = & $Py -c @"
import yaml, json, sys
with open(r'$YamlFile', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
sec = cfg.get('secrets', {}).get('$Secret')
if sec is None:
    print(f'ERROR: secret \"$Secret\" not defined in secrets.yaml', file=sys.stderr)
    sys.exit(2)
print(json.dumps(sec.get('consumers', [])))
"@

if ($LASTEXITCODE -ne 0) {
    Write-Host "[x] failed to read consumers for '$Secret' — see error above" -ForegroundColor Red
    exit 1
}

$consumers = $consumersRaw | ConvertFrom-Json
if (-not $consumers -or $consumers.Count -eq 0) {
    Write-Host "  [info] No consumers declared for '$Secret' — nothing to restart." -ForegroundColor Gray
    exit 0
}

Write-Host ""
Write-Host "── Restarting $($consumers.Count) consumer(s) for secret '$Secret' ──" -ForegroundColor Cyan
if ($DryRun) { Write-Host "  (DRY RUN — no actual restarts)" -ForegroundColor Yellow }

foreach ($c in $consumers) {
    $kind = $c.kind
    $name = $c.name
    $hotReload = $false
    if ($c.PSObject.Properties.Name -contains 'hot-reload') {
        $hotReload = [bool]$c.'hot-reload'
    }

    if ($hotReload) {
        Write-Host "  [skip] $kind  $name  (hot-reload: per-probe read)" -ForegroundColor DarkGray
        continue
    }

    Write-Host "  [..]   $kind  $name" -ForegroundColor Cyan -NoNewline
    if ($DryRun) {
        Write-Host "  (would restart)" -ForegroundColor Yellow
        continue
    }

    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        switch ($kind) {
            "docker" {
                $null = docker restart $name 2>&1
                Start-Sleep -Seconds 2
            }
            "wsl-systemd" {
                $null = wsl -d Ubuntu-24.04 -u root --exec bash -c "systemctl restart $name && sleep 3 && systemctl is-active $name" 2>&1
            }
            "win-pythonw" {
                # name is a regex matching CommandLine
                $procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $name }
                foreach ($p in $procs) {
                    $script = $p.CommandLine -replace '.*?"([^"]+\.py)".*', '$1'
                    if ($script -eq $p.CommandLine) {
                        # fallback: assume the regex matches a path fragment
                        $script = ($p.CommandLine -split '\s+' | Where-Object { $_ -match '\.py$' } | Select-Object -First 1) -replace '^"|"$', ''
                    }
                    $workDir = Split-Path -Parent $script
                    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 2
                    Start-Process -FilePath $PythonW -ArgumentList "`"$script`"" `
                                  -WorkingDirectory $workDir -WindowStyle Hidden
                    Start-Sleep -Seconds 2
                }
            }
            "win-service" {
                Restart-Service -Name $name -Force -ErrorAction Continue
            }
            "scheduled-task" {
                Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
                Start-ScheduledTask -TaskName $name
            }
            default {
                Write-Host "  unknown kind '$kind'" -ForegroundColor Red
                continue
            }
        }
        Write-Host "  → done" -ForegroundColor Green
    } catch {
        Write-Host "  → ERROR: $($_.Exception.Message)" -ForegroundColor Red
    } finally {
        $ErrorActionPreference = $prev
    }
}

Write-Host ""
Write-Host "[ok] Consumer restart cycle complete for '$Secret'" -ForegroundColor Green
