# scripts/rotate_cloudflare_tunnel.ps1
#
# Rotates the connector token for the cloudflared Windows service. The
# token is currently baked into the service binPath; we stop the
# service, update binPath with the new token via sc.exe, and restart.
#
# Tunnel:  9a2bb3c8-3b61-4c0d-b099-b2e10356a829
# Account: 3b9fa0bc0b8ba6f902d15016df534c7b
#
# Pre-req: get a new connector token from Cloudflare:
#   https://one.dash.cloudflare.com/3b9fa0bc0b8ba6f902d15016df534c7b/networks/tunnels/cfd_tunnel/9a2bb3c8-3b61-4c0d-b099-b2e10356a829
#   -> click "Edit" -> "Refresh token" (if visible)
#   OR copy a fresh install command from the tunnel page - it contains
#   the token in `cloudflared service install eyJ...`
#
# Requires admin (sc.exe config needs SERVICE_CHANGE_CONFIG access).
#
# Usage:  Run from an ELEVATED PowerShell prompt.
#         .\scripts\rotate_cloudflare_tunnel.ps1

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
$CfBin = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$Svc = "cloudflared"

# 0. Admin check
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[x] This script needs admin (sc.exe config requires SERVICE_CHANGE_CONFIG)." -ForegroundColor Red
    Write-Host "    Right-click PowerShell -> 'Run as administrator', then re-run." -ForegroundColor DarkGray
    exit 1
}

Write-Host ""
Write-Host "── Rotate Cloudflare Tunnel connector token ──" -ForegroundColor Cyan
Write-Host "Paste the new token (long eyJ...). Input is hidden." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "New token" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 100) {
    Write-Host "[x] Token too short - real connector tokens are 250+ chars" -ForegroundColor Red
    exit 1
}
if (-not $tok.StartsWith("eyJ")) {
    Write-Host "[x] Token doesn't start with 'eyJ' (base64-JWT prefix). Wrong paste?" -ForegroundColor Red
    exit 1
}

# 1. Parse token to verify it's for the expected tunnel (catches paste-from-different-tunnel mistake)
Write-Host ""
Write-Host "Decoding token to verify tunnel match..." -ForegroundColor Cyan
$tempTok = Join-Path $env:TEMP "new_cf_token.txt"
Set-Content -Path $tempTok -Value $tok -NoNewline -Encoding UTF8

$decodeScript = @'
import base64, json, sys
with open(sys.argv[1], encoding='utf-8-sig') as f: tok = f.read().strip()
# Connector tokens are base64-encoded JSON (NOT a JWT despite eyJ prefix)
pad = "=" * (-len(tok) % 4)
try:
    payload = json.loads(base64.b64decode(tok + pad))
except Exception as e:
    print(f"[x] Token is not valid base64-JSON: {e}", file=sys.stderr)
    sys.exit(2)
expected_account = "3b9fa0bc0b8ba6f902d15016df534c7b"
expected_tunnel  = "9a2bb3c8-3b61-4c0d-b099-b2e10356a829"
acct = payload.get("a")
tun  = payload.get("t")
print(f"  account: {acct}")
print(f"  tunnel:  {tun}")
if acct != expected_account:
    print(f"[x] account tag mismatch (expected {expected_account})", file=sys.stderr)
    sys.exit(3)
if tun != expected_tunnel:
    print(f"[x] tunnel id mismatch (expected {expected_tunnel})", file=sys.stderr)
    sys.exit(4)
print("  [ok] account + tunnel match the active service")
'@
$tempDecode = Join-Path $env:TEMP "cf_decode.py"
Set-Content -Path $tempDecode -Value $decodeScript -Encoding UTF8

try {
    & $Py $tempDecode $tempTok
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    aborting - service unchanged" -ForegroundColor Red
        exit 1
    }
} finally {
    Remove-Item -Path $tempDecode -ErrorAction SilentlyContinue
}

# 2. WCM mirror BEFORE touching the service - if anything fails, we can recover
Write-Host ""
Write-Host "Mirroring in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','cloudflared_token','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM mirror failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/cloudflared_token" -ForegroundColor Green

# 3. Update service binPath
$newBinPath = "`"$CfBin`" tunnel run --token $tok"

Write-Host ""
Write-Host "Stopping cloudflared service..." -ForegroundColor Cyan
sc.exe stop $Svc | Out-Null
# Wait for STOPPED state (sc stop returns before service is actually stopped)
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $state = (Get-Service $Svc).Status
} while ($state -ne 'Stopped' -and (Get-Date) -lt $deadline)
if ($state -ne 'Stopped') {
    Write-Host "[x] Service did not stop within 30s (state=$state)" -ForegroundColor Red
    exit 1
}
Write-Host "  [ok] service stopped" -ForegroundColor Green

Write-Host ""
Write-Host "Updating service binPath..." -ForegroundColor Cyan
# Note: sc.exe needs `binPath= ` with a SPACE after the equals
$scResult = & sc.exe config $Svc binPath= $newBinPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "[x] sc.exe config failed: $scResult" -ForegroundColor Red
    Write-Host "    starting old binPath again..." -ForegroundColor DarkGray
    sc.exe start $Svc | Out-Null
    exit 1
}
Write-Host "  [ok] binPath updated" -ForegroundColor Green

Write-Host ""
Write-Host "Starting cloudflared service..." -ForegroundColor Cyan
sc.exe start $Svc | Out-Null
Start-Sleep -Seconds 4
$state = (Get-Service $Svc).Status
if ($state -ne 'Running') {
    Write-Host "[x] Service didn't start (state=$state)" -ForegroundColor Red
    Write-Host "    Check Event Viewer or run: cloudflared tunnel run --token <token>  manually" -ForegroundColor DarkGray
    exit 1
}
Write-Host "  [ok] service running" -ForegroundColor Green

# 4. Wait briefly for connector to register, then check
Write-Host ""
Write-Host "Waiting for tunnel connection..." -ForegroundColor Cyan
Start-Sleep -Seconds 8

# Verify by checking outbound established connections to cloudflare
$cf = Get-Process cloudflared -ErrorAction SilentlyContinue
if ($cf) {
    $conns = Get-NetTCPConnection -OwningProcess $cf.Id -State Established -ErrorAction SilentlyContinue
    if ($conns) {
        Write-Host "  [ok] cloudflared has $($conns.Count) established connection(s) to Cloudflare edge" -ForegroundColor Green
    } else {
        Write-Host "  [warn] cloudflared running but no established connections yet (give it 30s)" -ForegroundColor Yellow
    }
}

# Cleanup temp file (token contents)
Remove-Item -Path $tempTok -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[ok] Done. Test by hitting whatever hostname the tunnel publishes." -ForegroundColor Green
Write-Host "    Old connector will appear OFFLINE in CF dashboard - safe to delete." -ForegroundColor DarkGray

$tok = "x" * 300
Remove-Variable tok -ErrorAction SilentlyContinue
