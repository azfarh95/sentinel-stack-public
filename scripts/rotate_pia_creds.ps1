# scripts/rotate_pia_creds.ps1
#
# Securely capture PIA credentials + the Tailscale auth key for the
# pia-exit / tailscale-pia containers. Inputs are read via Read-Host
# -AsSecureString so nothing lands in shell history or this chat's log.
#
# Usage:
#   .\scripts\rotate_pia_creds.ps1
#
# What it does:
#   1. Prompts for PIA username (hidden), PIA password (hidden), region
#      (visible), optional dedicated-IP token (hidden), and Tailscale auth
#      key for the exit node (hidden).
#   2. Stores all five in Windows Credential Manager under
#      sentinel-miniapp/<key>.
#   3. Runs sync_env_from_wcm.ps1 so .env.local picks them up.
#   4. Verifies values landed in .env.local (last 4 chars only — never
#      echoes the actual secret).
#
# After running:
#   docker compose --env-file .env.local --profile vpn up -d pia-exit tailscale-pia

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Py)) { $Py = (Get-Command py).Source }

function Read-Secret($label) {
    $sec  = Read-Host $label -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $val  = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null
    return $val
}

function Store-WCM($key, $value) {
    if ([string]::IsNullOrEmpty($value)) {
        Write-Host "  (empty — skipping $key)" -ForegroundColor DarkGray
        return
    }
    & $Py -c "import keyring; keyring.set_password('sentinel-miniapp', '$key', '$value')"
    if ($LASTEXITCODE -ne 0) { throw "WCM write failed for $key" }
    Write-Host "  ✓ stored: sentinel-miniapp/$key" -ForegroundColor Green
}

Write-Host ""
Write-Host "── Rotate PIA + Tailscale exit-node credentials ──" -ForegroundColor Cyan
Write-Host ""
Write-Host "Inputs are hidden. Press Enter to skip optional fields." -ForegroundColor Yellow
Write-Host ""

# PIA credentials
$piaUser = Read-Secret "PIA username           "
$piaPass = Read-Secret "PIA password           "

Write-Host ""
Write-Host "PIA region (visible, e.g. 'Singapore', 'Tokyo', 'US East'). Default: Singapore"
$piaRegion = Read-Host "PIA region [Singapore]  "
if ([string]::IsNullOrWhiteSpace($piaRegion)) { $piaRegion = "Singapore" }

Write-Host ""
Write-Host "PIA dedicated-IP token (hidden, optional). Find at:"
Write-Host "  https://www.privateinternetaccess.com/account/dedicated-ip" -ForegroundColor DarkGray
$piaDIPToken = Read-Secret "PIA dedicated-IP token "

Write-Host ""
Write-Host "Tailscale auth key for the exit node (hidden)."
Write-Host "  Generate at: https://login.tailscale.com/admin/settings/keys" -ForegroundColor DarkGray
Write-Host "  Suggested settings: reusable=NO, ephemeral=NO, tags=tag:owner, expiry=longest" -ForegroundColor DarkGray
$tsKey = Read-Secret "TS_AUTHKEY_PIA_EXIT    "

# Validate minimum input
if (-not $piaUser -or -not $piaPass) {
    Write-Host "✗ PIA username + password are required." -ForegroundColor Red
    exit 1
}
if (-not $tsKey) {
    Write-Host "✗ Tailscale auth key is required (containers won't join the mesh without it)." -ForegroundColor Red
    exit 1
}
if (-not $tsKey.StartsWith("tskey-")) {
    Write-Host "⚠ Tailscale key doesn't start with 'tskey-' — likely a paste error. Aborting." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "── Storing in Windows Credential Manager ──" -ForegroundColor Cyan
Store-WCM "pia_user"                  $piaUser
Store-WCM "pia_password"              $piaPass
Store-WCM "pia_region"                $piaRegion
Store-WCM "pia_dedicated_ip_token"    $piaDIPToken
Store-WCM "ts_authkey_pia_exit"       $tsKey

Write-Host ""
Write-Host "── Re-syncing .env.local from WCM ──" -ForegroundColor Cyan
& (Join-Path $RepoRoot "scripts\sync_env_from_wcm.ps1")

Write-Host ""
Write-Host "── Verifying .env.local has the new keys ──" -ForegroundColor Cyan
$envContent = Get-Content (Join-Path $RepoRoot ".env.local") -Raw
foreach ($k in @("PIA_USER", "PIA_PASSWORD", "PIA_REGION", "TS_AUTHKEY_PIA_EXIT")) {
    $line = ($envContent -split "`n") | Where-Object { $_ -match "^$k=" }
    if (-not $line) {
        Write-Host "  ✗ $k missing from .env.local — sync_env_from_wcm.ps1 may need updating to include this key" -ForegroundColor Red
        continue
    }
    $val = $line -replace "^$k=", ""
    $tail = if ($val.Length -ge 4) { "...$($val.Substring($val.Length - 4))" } else { "(short)" }
    Write-Host "  ✓ $k=$tail" -ForegroundColor Green
}

# Wipe local vars (best-effort)
$piaUser = $piaPass = $piaDIPToken = $tsKey = ("x" * 80)
Remove-Variable piaUser, piaPass, piaDIPToken, tsKey -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "── Done. Bring up the exit node: ──" -ForegroundColor Green
Write-Host "  docker compose --env-file .env.local --profile vpn up -d pia-exit tailscale-pia" -ForegroundColor Yellow
Write-Host ""
Write-Host "After it's healthy, on your phone/laptop Tailscale client:" -ForegroundColor Yellow
Write-Host "  Settings → Exit nodes → sentinel-pia-exit → Use" -ForegroundColor Yellow
