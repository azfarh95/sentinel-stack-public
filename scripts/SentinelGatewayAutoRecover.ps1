# SentinelGatewayAutoRecover.ps1
# Self-heals the OpenClaw gateway (lives inside WSL Ubuntu-24.04) when it goes down.
# Tiered recovery (cheap -> heavy), so it never bounces Docker unless WSL is truly wedged:
#   Tier 1: WSL responsive + gateway inactive -> systemctl restart openclaw-gateway (no Docker bounce)
#   Tier 2: WSL itself unreachable (E_UNEXPECTED) -> wsl --shutdown + warm (heavy; bounces Docker, auto-recovers)
# Rails: 15s verify-twice debounce; 20-min cooldown on the heavy path; logging.
# ASCII-only + PS 5.1-safe (scheduled tasks run Windows PowerShell 5.1).
$ErrorActionPreference = 'SilentlyContinue'
$Distro = 'Ubuntu-24.04'
$Log    = 'C:\Users\azfar\metamcp-local\logs\gateway_autorecover.log'
$Stamp  = 'C:\Users\azfar\metamcp-local\logs\gateway_recover_last.txt'

function Write-Log([string]$m) {
  $ts = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss')
  ($ts + 'Z ' + $m) | Out-File -FilePath $Log -Append -Encoding ascii
}

function Test-WslUp {
  $null = wsl.exe -d $Distro -- true 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Test-GatewayActive {
  $r = (wsl.exe -d $Distro -u root systemctl is-active openclaw-gateway 2>$null | Out-String).Trim()
  return ($r -eq 'active')
}

# Fast path: gateway healthy -> nothing to do.
if ((Test-WslUp) -and (Test-GatewayActive)) { exit 0 }

# Debounce: re-check after 15s so a momentary blip / SIGUSR1 reload doesn't trip us.
Start-Sleep -Seconds 15
$wslUp = Test-WslUp
if ($wslUp -and (Test-GatewayActive)) { exit 0 }

if ($wslUp) {
  # Tier 1 -- WSL is fine, gateway died: cheap restart, no Docker bounce.
  Write-Log 'gateway inactive (WSL up) -> systemctl restart openclaw-gateway'
  $null = wsl.exe -d $Distro -u root systemctl restart openclaw-gateway 2>$null
  Start-Sleep -Seconds 6
  if (Test-GatewayActive) { Write-Log 'RECOVERED via service restart' }
  else { Write-Log 'service restart did NOT recover; next run escalates if WSL wedges' }
  exit 0
}

# Tier 2 -- WSL itself wedged (E_UNEXPECTED): heavy path, with cooldown.
$now = (Get-Date).ToUniversalTime()
if (Test-Path $Stamp) {
  $lastTxt = (Get-Content $Stamp -Raw)
  if ($lastTxt) { $lastTxt = $lastTxt.Trim() }
  $last = [DateTime]::MinValue
  if ([DateTime]::TryParse($lastTxt, [ref]$last)) {
    if (($now - $last.ToUniversalTime()).TotalMinutes -lt 20) {
      Write-Log 'WSL wedged but within 20-min cooldown -> skipping wsl --shutdown'
      exit 0
    }
  }
}
Write-Log 'WSL WEDGED (E_UNEXPECTED) -> wsl --shutdown + warm'
$now.ToString('o') | Out-File -FilePath $Stamp -Encoding ascii
$null = wsl.exe --shutdown 2>$null
Start-Sleep -Seconds 6
$null = wsl.exe -d $Distro -- true 2>$null
Start-Sleep -Seconds 8
if (Test-GatewayActive) { Write-Log 'RECOVERED via wsl --shutdown + warm' }
else { Write-Log 'STILL DOWN after wsl --shutdown + warm -- manual attention needed' }
exit 0
