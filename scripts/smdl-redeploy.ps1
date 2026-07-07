<#
.SYNOPSIS
  One-shot SMDL redeploy that automates the tailscale-serve :8096 dance.

.DESCRIPTION
  smdl publishes 127.0.0.1:8096, but `tailscale serve` holds tcp :8096 and
  forwards it to localhost:8096. On Windows Docker Desktop, Docker's loopback
  bind for 8096 silently fails while serve holds the port - so every smdl
  *recreate* (compose/env/image change) needs: serve off -> recreate -> serve on.
  This does that in one go, with a health gate, and ALWAYS restores serve (the
  serve re-add is in a finally{} so a mid-run failure can never leave a 502).

  Most code deploys no longer need this - app/ is bind-mounted with
  uvicorn --reload (docker-compose.yml), so editing app code hot-reloads in
  place. Use this only for compose/env/dependency/image changes.

  Writes a transcript to scripts\smdl-redeploy.log (so a scheduled-task run that
  swallows console output is still diagnosable).

  Run once elevated to register as the no-elevation-needed task:
      pwsh -File scripts\register-smdl-redeploy-task.ps1
  then trigger anytime with:  schtasks /run /tn SentinelSMDLRedeploy

.PARAMETER Build
  Rebuild the smdl image before recreating (dependency/Dockerfile changes).
#>
[CmdletBinding()]
param(
  [switch]$Build,
  [string]$StackDir = 'C:\Users\azfar\metamcp-local',
  [int]$Port = 8096,
  [string]$Service = 'smdl'
)

# Native-command friendly: don't let an exe's stderr/non-zero abort the run.
$ErrorActionPreference = 'Continue'
$log = Join-Path $StackDir 'scripts\smdl-redeploy.log'
try { Start-Transcript -Path $log -Append | Out-Null } catch { }
function Log($m) { Write-Host ("[smdl-redeploy {0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

function Resolve-Exe([string]$name, [string[]]$fallbacks) {
  $c = (Get-Command $name -ErrorAction SilentlyContinue).Source
  if ($c) { return $c }
  foreach ($f in $fallbacks) { if (Test-Path $f) { return $f } }
  return $null
}

$ts = Resolve-Exe 'tailscale.exe' @('C:\Program Files\Tailscale\tailscale.exe')
$docker = Resolve-Exe 'docker.exe' @('C:\Program Files\Docker\Docker\resources\bin\docker.exe')
$envFile = Join-Path $StackDir '.env.local'

Log ("start: ts={0} docker={1} stackdir={2} build={3}" -f $ts, $docker, $StackDir, $Build)
if (-not $ts)     { Log 'ERROR: tailscale.exe not found'; try { Stop-Transcript | Out-Null } catch {}; exit 2 }
if (-not $docker) { Log 'ERROR: docker.exe not found';    try { Stop-Transcript | Out-Null } catch {}; exit 3 }
if (-not (Test-Path $envFile)) { Log "ERROR: env file not found: $envFile"; try { Stop-Transcript | Out-Null } catch {}; exit 4 }

Set-Location $StackDir
$rc = 0
try {
  Log "clearing tailscale serve :$Port ..."
  & $ts serve --tls-terminated-tcp=$Port off 2>&1 | ForEach-Object { Log "  ts> $_" }

  if ($Build) {
    Log "building $Service image ..."
    & $docker compose --env-file $envFile --profile media build $Service 2>&1 | ForEach-Object { Log "  $_" }
  }

  Log "recreating $Service ..."
  & $docker compose --env-file $envFile --profile media up -d $Service 2>&1 | ForEach-Object { Log "  $_" }

  Log "waiting for http://localhost:$Port/health ..."
  $ok = $false
  foreach ($i in 1..30) {
    try {
      $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://localhost:$Port/health"
      if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
  }
  if ($ok) { Log 'health 200 OK' } else { Log "WARNING: health never 200 - check 'docker logs $Service'"; $rc = 5 }
}
catch {
  Log ("ERROR: " + $_.Exception.Message); $rc = 1
}
finally {
  # ALWAYS restore serve, even if the recreate failed - never leave a 502.
  # Backend pinned to 127.0.0.1 (NOT localhost): on Windows localhost->::1 first,
  # and a half-open Docker-Desktop IPv6 forwarder makes a localhost target hang
  # (the 2026-06-14 wedge). 127.0.0.1 forces IPv4 and is immune.
  Log "re-adding tailscale serve :$Port -> 127.0.0.1:$Port ..."
  & $ts serve --bg --tls-terminated-tcp=$Port "tcp://127.0.0.1:$Port" 2>&1 | ForEach-Object { Log "  ts> $_" }
  Log "docker port ${Service}:"
  & $docker port $Service 2>&1 | ForEach-Object { Log "  $_" }
  Log ("done (rc=$rc)")
  try { Stop-Transcript | Out-Null } catch { }
}
exit $rc
