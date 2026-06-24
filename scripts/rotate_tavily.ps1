# scripts/rotate_tavily.ps1
#
# Rotates the Tavily API key (search MCP). Key lives inside the MetaMCP
# Postgres mcp_servers.env JSON column for UUID 71ca39d2-... ; restart
# MetaMCP container so it re-reads the row.
#
# Pre-req: regenerate the key at https://app.tavily.com/home (API Keys tab)
#          then paste it at the hidden prompt.
#
# Usage:  .\scripts\rotate_tavily.ps1

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
$TavilyUuid = "71ca39d2-0165-46a6-9f1b-6c9bef1392b5"

Write-Host ""
Write-Host "── Rotate Tavily API key ──" -ForegroundColor Cyan
Write-Host "Paste the new key from app.tavily.com. Input is hidden." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "New key" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$tok = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null

if (-not $tok -or $tok.Length -lt 20) {
    Write-Host "[x] Key looks too short - aborting" -ForegroundColor Red
    exit 1
}

# 1. Validate via a tiny search call. Use Invoke-RestMethod (not curl.exe) -
#    PowerShell strips inner double quotes when passing -d "{...}" to native
#    exes, so curl receives malformed JSON. IRM serializes natively.
Write-Host ""
Write-Host "Validating via /search..." -ForegroundColor Cyan
try {
    $payload = @{ query = "ping"; max_results = 1 } | ConvertTo-Json -Compress
    $resp = Invoke-RestMethod -Method Post `
        -Uri "https://api.tavily.com/search" `
        -Headers @{ "Authorization" = "Bearer $tok"; "Content-Type" = "application/json" } `
        -Body $payload `
        -TimeoutSec 12 -ErrorAction Stop
    if (-not $resp.results) {
        Write-Host "[x] Tavily response missing 'results': $($resp | ConvertTo-Json -Compress)" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [ok] Key validated (got $($resp.results.Count) results)" -ForegroundColor Green
} catch {
    Write-Host "[x] Tavily rejected the key: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.Exception.Response) {
        $stream = $_.Exception.Response.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream)
        Write-Host "    body: $($reader.ReadToEnd())" -ForegroundColor DarkGray
    }
    exit 1
}

# 2. Mirror in WCM (for any future consumer + audit trail)
Write-Host ""
Write-Host "Storing in Windows Credential Manager..." -ForegroundColor Cyan
& $Py -c "import keyring; keyring.set_password('sentinel-miniapp','tavily_api_key','$tok')"
if ($LASTEXITCODE -ne 0) { throw "WCM update failed" }
Write-Host "  [ok] WCM updated: sentinel-miniapp/tavily_api_key" -ForegroundColor Green

# 3a. Update MetaMCP Postgres (for the Tavily MCP server)
Write-Host ""
Write-Host "Updating mcp_servers.env in metamcp-pg..." -ForegroundColor Cyan
$sql = "UPDATE mcp_servers SET env = jsonb_set(env, '{TAVILY_API_KEY}', to_jsonb('$tok'::text)) WHERE uuid = '$TavilyUuid';"
docker exec metamcp-pg psql -U metamcp_user -d metamcp_db -c $sql

# 3b. Update OpenClaw's openclaw.json (separate webSearch.apiKey field).
# OpenClaw has its OWN Tavily-backed web_search tool with its OWN key
# slot. Missing this update -> bot's web_search calls 401, model falls
# back to fabricating + mis-attributing results "via Tavily". The
# walk-and-replace approach is resilient to openclaw rewriting the
# file layout.
Write-Host ""
Write-Host "Updating openclaw.json webSearch.apiKey (WSL path)..." -ForegroundColor Cyan
$tempK = Join-Path $env:TEMP "new_tavily_key.txt"
Set-Content -Path $tempK -Value $tok -NoNewline -Encoding UTF8
$wslTok = "/mnt/c/Users/azfar/AppData/Local/Temp/new_tavily_key.txt"
try {
    wsl -d Ubuntu-24.04 -u azfar --exec bash -c @"
python3 - << 'PYEOF'
import json
with open('$wslTok', encoding='utf-8-sig') as f: new_key = f.read().strip()
path = '/home/azfar/.openclaw/openclaw.json'
with open(path, encoding='utf-8-sig') as f: cfg = json.load(f)
def walk(d, count=[0]):
    if isinstance(d, dict):
        for k, v in list(d.items()):
            if k == 'apiKey' and isinstance(v, str) and v.startswith('tvly-'):
                d[k] = new_key; count[0] += 1
            else: walk(v, count)
    elif isinstance(d, list):
        for x in d: walk(x, count)
    return count[0]
n = walk(cfg)
with open(path, 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=2)
print(f'  openclaw.json: replaced {n} tavily apiKey field(s)')
PYEOF
"@
} finally {
    Remove-Item -Path $tempK -ErrorAction SilentlyContinue
}
if ($LASTEXITCODE -ne 0) { throw "Postgres update failed" }

# 4. Restart consumers. BOTH need to reload the new key:
#    - MetaMCP (for the Tavily MCP server reading from mcp_servers.env)
#    - openclaw-gateway (caches openclaw.json at startup, holds old key
#                        in-memory across runtime)
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    Write-Host ""
    Write-Host "Restarting MetaMCP container..." -ForegroundColor Cyan
    docker restart metamcp 2>&1 | Select-Object -Last 2 | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "Restarting openclaw-gateway..." -ForegroundColor Cyan
    wsl -d Ubuntu-24.04 -u root --exec bash -c "systemctl restart openclaw-gateway && sleep 4 && systemctl is-active openclaw-gateway"
} finally {
    $ErrorActionPreference = $prev
}

Write-Host ""
Write-Host "[ok] Done. Next Tavily MCP call (via MetaMCP) will use the new key." -ForegroundColor Green
$tok = "x" * 80
Remove-Variable tok -ErrorAction SilentlyContinue
