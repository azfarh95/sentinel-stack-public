﻿# scripts/rotate_lmstudio_api.ps1
#
# Rotates the LM Studio Local Server API key used by OpenClaw to call
# lmstudio:default (qwen3.6-27b primary, gemma fallbacks). Key is
# stored in:
#   1. WCM: sentinel-openclaw/lmstudio_api_key (audit trail)
#   2. /home/azfar/.openclaw/agents/main/agent/auth-profiles.json  (WSL-NATIVE)
#      ->  profiles -> lmstudio:default -> key
#      The Windows-side C:\Users\azfar\.openclaw\ is a vestige and NOT read.
#
# Also clears infer-bridge's cached key + restarts OpenClaw gateway.
#
# Pre-req: in LM Studio app: Developer tab -> "Local Server" section ->
#   gear icon -> "API Keys" (or "Authentication") -> regenerate the key.
#   LM Studio doesn't expose a deep link; the path is consistent.
#
# Usage:  .\scripts\rotate_lmstudio_api.ps1

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
# OpenClaw runs as a WSL2 systemd service and reads from the LINUX-NATIVE
# path (/home/azfar/.openclaw/...), NOT from C:\Users\azfar\.openclaw\
# (which is a vestigial copy from an older install). The two have different
# inodes - they're separate files. Writing to the Windows path silently
# updates a file that NOTHING alive reads.
#
# Three WSL files hold the LM Studio key and ALL must be updated together,
# otherwise the gateway picks up the stale one and you get silent 401s:
#   - auth-profiles.json -> profiles.lmstudio:default.key
#   - openclaw.json      -> models.providers.lmstudio.apiKey  (highest priority)
#   - models.json        -> apiKey (less authoritative but kept consistent)
$AuthProfileWsl = "/home/azfar/.openclaw/agents/main/agent/auth-profiles.json"
$OpenclawJsonWsl = "/home/azfar/.openclaw/openclaw.json"
$ModelsJsonWsl   = "/home/azfar/.openclaw/agents/main/agent/models.json"

Write-Host ""
Write-Host "── Rotate LM Studio API key ──" -ForegroundColor Cyan
Write-Host "Paste the new key from LM Studio (Developer tab). Input is hidden." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "New key" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 20) {
    Write-Host "[x] Key looks too short - aborting (LM Studio keys start with 'sk-lm-')" -ForegroundColor Red
    exit 1
}

# 1. Validate by calling /v1/models with the new key. Use Invoke-RestMethod
#    (curl.exe -w combined with array semantics caused false negatives where
#    a real 200 was treated as failure - PowerShell evaluates -notmatch on
#    each line of multi-line output independently).
Write-Host ""
Write-Host "Validating against http://127.0.0.1:1234/v1/models ..." -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod -Method Get `
        -Uri "http://127.0.0.1:1234/v1/models" `
        -Headers @{ "Authorization" = "Bearer $tok" } `
        -TimeoutSec 10 -ErrorAction Stop
    if (-not $resp.data) {
        Write-Host "[x] LM Studio response missing 'data' field" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [ok] Key validated ($($resp.data.Count) models visible)" -ForegroundColor Green
} catch {
    Write-Host "[x] LM Studio rejected the key: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "    Is LM Studio's local server running? Is API auth enabled?" -ForegroundColor DarkGray
    exit 1
}

# 2. WCM mirror
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-openclaw','lmstudio_api_key','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-openclaw/lmstudio_api_key" -ForegroundColor Green

# 3. Update auth-profiles.json (the actual file OpenClaw reads). Use python
#    so the JSON stays well-formed regardless of escape edge-cases. Stage
#    via temp file - same lesson as rotate_agent_bot.ps1: nested shell
#    quoting eats single quotes silently.
Write-Host ""
Write-Host "Updating auth-profiles.json (WSL path)..." -ForegroundColor Cyan
# Stage token to a Windows TEMP file (accessible from WSL via /mnt/c/...),
# then run a python here-doc inside WSL to read it and update the
# WSL-native auth-profiles.json. Same lesson as rotate_agent_bot.ps1:
# avoid nested-shell quoting of the secret on the command line.
$tempTok = Join-Path $env:TEMP "new_lmstudio_key.txt"
Set-Content -Path $tempTok -Value $tok -NoNewline -Encoding UTF8
$wslTokPath = "/mnt/c/Users/azfar/AppData/Local/Temp/new_lmstudio_key.txt"
try {
    wsl -d Ubuntu-24.04 -u azfar --exec bash -c @"
python3 - << 'PYEOF'
import json

with open('$wslTokPath', encoding='utf-8-sig') as f: tok = f.read().strip()

# 3a. auth-profiles.json -> profiles.lmstudio:default.key
with open('$AuthProfileWsl', encoding='utf-8-sig') as f: cfg = json.load(f)
cfg['profiles']['lmstudio:default']['key'] = tok
with open('$AuthProfileWsl', 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=2)
print(f'  auth-profiles.json    updated')

# 3b. openclaw.json -> walk all dict nodes, replace any apiKey that looks
#     like an LM Studio key. The actual path is models.providers.lmstudio.apiKey
#     but openclaw rewrites this file on its own so the layout may shift.
def replace_lm_keys(d, count=[0]):
    if isinstance(d, dict):
        for k, v in list(d.items()):
            if k == 'apiKey' and isinstance(v, str) and v.startswith('sk-lm-'):
                d[k] = tok
                count[0] += 1
            else:
                replace_lm_keys(v, count)
    elif isinstance(d, list):
        for item in d:
            replace_lm_keys(item, count)
    return count[0]

with open('$OpenclawJsonWsl', encoding='utf-8-sig') as f: oc = json.load(f)
n = replace_lm_keys(oc)
with open('$OpenclawJsonWsl', 'w', encoding='utf-8') as f: json.dump(oc, f, indent=2)
print(f'  openclaw.json         updated ({n} apiKey field(s))')

# 3c. models.json (same walk)
with open('$ModelsJsonWsl', encoding='utf-8-sig') as f: mj = json.load(f)
n = replace_lm_keys(mj)
with open('$ModelsJsonWsl', 'w', encoding='utf-8') as f: json.dump(mj, f, indent=2)
print(f'  models.json           updated ({n} apiKey field(s))')

print(f'  key suffix: ...{tok[-6:]}')
PYEOF
"@
} finally {
    Remove-Item -Path $tempTok -ErrorAction SilentlyContinue
}

# 4. Restart OpenClaw gateway so it re-reads auth-profiles.json
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting openclaw-gateway..." -ForegroundColor Cyan
    wsl -d Ubuntu-24.04 -u root --exec bash -c "systemctl restart openclaw-gateway && sleep 4 && systemctl is-active openclaw-gateway"
} finally {
    $ErrorActionPreference = $prev
}

# 5. Restart infer-bridge (also calls LM Studio with the same key)
Write-Host ""
Write-Host "Restarting infer-bridge..." -ForegroundColor Cyan
$ib = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'infer_bridge\.py' } | Select-Object -First 1
if ($ib) {
    Stop-Process -Id $ib.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
$pythonw = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\pythonw.exe"
Start-Process -FilePath $pythonw -ArgumentList "C:\Users\azfar\metamcp-local\infer_bridge.py" -WindowStyle Hidden
Start-Sleep -Seconds 2
$newIb = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'infer_bridge\.py' } | Select-Object -First 1
if ($newIb) { Write-Host "  [ok] infer-bridge relaunched (PID $($newIb.ProcessId))" -ForegroundColor Green }

# 6. Mirror LM Studio key to sentinel-watchdog/lm_api_key + restart watchdog.
# The watchdog ALSO probes /v1/models for its health page. Without this step,
# the watchdog spams false "LM Studio API down (HTTP 401 [config])" alerts
# every 30 min until manually restarted - which is exactly what happened
# on 2026-05-11 and led to the per-probe refactor (Phase B). Until that
# lands, this restart is the band-aid.
Write-Host ""
Write-Host "Mirroring key to sentinel-watchdog and bouncing watchdog..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-watchdog','lm_api_key','$tok')"
$wd = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'watchdog\\watchdog\.py' } | Select-Object -First 1
if ($wd) {
    Stop-Process -Id $wd.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
Start-Process -FilePath $pythonw `
              -ArgumentList "C:\Users\azfar\metamcp-local\watchdog\watchdog.py" `
              -WorkingDirectory "C:\Users\azfar\metamcp-local\watchdog" `
              -WindowStyle Hidden
Start-Sleep -Seconds 3
$newWd = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'watchdog\\watchdog\.py' } | Select-Object -First 1
if ($newWd) { Write-Host "  [ok] watchdog relaunched (PID $($newWd.ProcessId))" -ForegroundColor Green }

Write-Host ""
Write-Host "[ok] Done. Send @YourSentinelBot a test prompt to confirm round-trip." -ForegroundColor Green
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue
