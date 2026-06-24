# scripts/sanity_check.ps1
#
# Comprehensive Sentinel sanity + stability check.
# Read-only by default — nothing is deleted. Cleanup hints printed inline.
#
# Companion to docs/CACHE_REGISTRY.md.
#
# Usage:
#   .\scripts\sanity_check.ps1                       # all sections
#   .\scripts\sanity_check.ps1 -Section endpoints    # just one
#
# Sections: legacy | parallel | endpoints | drift | wcm | disk |
#           resources | volumes | dupes | json-yaml | watchdog-poll |
#           backups | ps-bom | tailscale | dns | metamcp-migrations |
#           leak-scan

param([string]$Section = "all")

$ErrorActionPreference = "Continue"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AllSections = "all"

function Section($num, $title) {
    Write-Host ""
    Write-Host ("═" * 68) -ForegroundColor Cyan
    Write-Host "  $num. $title" -ForegroundColor Cyan
    Write-Host ("═" * 68) -ForegroundColor Cyan
}
function Ok($msg)    { Write-Host "  [ok]    $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "  [warn]  $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "  [FAIL]  $msg" -ForegroundColor Red }
function Info($msg)  { Write-Host "  $msg" -ForegroundColor Gray }

function Run($name) { ($Section -eq "all") -or ($Section -eq $name) }

# ── 1. LEGACY FILES ─────────────────────────────────────────────────────────
if (Run "legacy") {
    Section 1 "LEGACY / STALE FILES"
    Write-Host "── WSL .openclaw legacy files ──"
    $legacy = wsl -d Ubuntu-24.04 -u azfar -- bash -c "ls /home/azfar/.openclaw/ 2>/dev/null | grep -E 'clobbered|\.broken$|\.bak\.[0-9]|\.new$|\.orig$' || echo 'NONE'"
    if ($legacy -match 'NONE') { Ok "no legacy files" } else { $legacy | ForEach-Object { Warn $_ } }
    Write-Host ""
    Write-Host "── Windows-side stub C:\Users\azfar\.openclaw ──"
    if (Test-Path "C:\Users\azfar\.openclaw") {
        Warn "exists (vestigial stub, NOT read by live WSL service)"
    } else { Ok "absent" }
}

# ── 2. PARALLEL INSTALLS ────────────────────────────────────────────────────
if (Run "parallel") {
    Section 2 "PARALLEL INSTALLS"
    Write-Host "── tailscaled processes ──"
    $ts = @(Get-Process tailscaled -ErrorAction SilentlyContinue)
    if ($ts.Count -le 1) { Ok "$($ts.Count) tailscaled" } else { Warn "$($ts.Count) tailscaled (1 expected)" }
    Write-Host ""
    Write-Host "── docker-compose-*.yml variants ──"
    $composes = @(Get-ChildItem "$RepoRoot\docker-compose*.yml" -ErrorAction SilentlyContinue)
    if ($composes.Count -eq 1) { Ok "single compose: $($composes[0].Name)" }
    else { $composes | ForEach-Object { Warn $_.Name } }
}

# ── 3. ENDPOINT HANDSHAKE ───────────────────────────────────────────────────
if (Run "endpoints") {
    Section 3 "ENDPOINT HANDSHAKE"
    $lmKey = (& $Py -c "import keyring; print(keyring.get_password('sentinel-watchdog','lm_api_key') or '')").Trim()
    $endpoints = @(
        @{n="MetaMCP";          u="http://127.0.0.1:12008/health"},
        @{n="Reminders MCP";    u="http://127.0.0.1:8087/health"},
        @{n="SMDL MCP";         u="http://127.0.0.1:8088/health"},
        @{n="Google WS MCP";    u="http://127.0.0.1:8089/health"},
        @{n="Maps MCP";         u="http://127.0.0.1:8090/health"},
        @{n="GitHub MCP";       u="http://127.0.0.1:8091/health"; exp=401},
        @{n="Memory MCP";       u="http://127.0.0.1:8092/health"},
        @{n="OneDrive MCP";     u="http://127.0.0.1:8093/health"},
        @{n="Translate MCP";    u="http://127.0.0.1:8094/health"},
        @{n="Vaultwarden";      u="http://127.0.0.1:8085/alive"},
        @{n="Infer Bridge";     u="http://127.0.0.1:8095/infer_status"},
        @{n="SMDL standalone";  u="http://127.0.0.1:8096/health"},
        @{n="Sentinel Bridge";  u="http://127.0.0.1:8098/api/auth/status"; exp=401},
        @{n="Playwright proxy"; u="http://127.0.0.1:8932/sse"},
        @{n="LM Studio";        u="http://127.0.0.1:1234/v1/models"; bearer=$lmKey}
    )
    foreach ($ep in $endpoints) {
        $code = "DOWN"
        $h = if ($ep.bearer) { @{Authorization="Bearer $($ep.bearer)"} } else { @{} }
        try {
            $r = Invoke-WebRequest -Uri $ep.u -Headers $h -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
            $code = $r.StatusCode
        } catch { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } }
        $expected = if ($ep.exp) { $ep.exp } else { 200 }
        if ($code -eq $expected -or $code -eq 200) { Ok ("{0,-22} HTTP {1}" -f $ep.n, $code) }
        else { Fail ("{0,-22} HTTP {1} (expected {2})" -f $ep.n, $code, $expected) }
    }
    Write-Host ""
    Write-Host "── External APIs ──"
    foreach ($p in @(
        @{n="Tavily"; t={try{$k=(& $Py -c "import keyring;print(keyring.get_password('sentinel-miniapp','tavily_api_key') or '')").Trim();$null=Invoke-RestMethod -Method Post -Uri "https://api.tavily.com/search" -Headers @{Authorization="Bearer $k";"Content-Type"="application/json"} -Body '{"query":"ping","max_results":1}' -TimeoutSec 10 -ErrorAction Stop;$true}catch{$false}}},
        @{n="Telegram Agent bot"; t={try{$k=(& $Py -c "import keyring;print(keyring.get_password('sentinel-miniapp','telegram_bot_token') or '')").Trim();$null=Invoke-RestMethod -Uri "https://api.telegram.org/bot$k/getMe" -TimeoutSec 8 -ErrorAction Stop;$true}catch{$false}}}
    )) { if (& $p.t) { Ok $p.n } else { Fail $p.n } }
}

# ── 4. CACHE DRIFT ──────────────────────────────────────────────────────────
if (Run "drift") {
    Section 4 "CACHE DRIFT (WCM canonical vs embedded values)"
    & $Py -c @"
import keyring, json
def cmp(label, wcm, file_v, where):
    suf_w = wcm[-6:] if wcm else '<empty>'
    suf_f = file_v[-6:] if file_v else '<empty>'
    if wcm == file_v: print(f'  [ok]    {label:42}  ...{suf_w}')
    else:             print(f'  [DRIFT] {label:42}  WCM=...{suf_w}  {where}=...{suf_f}')

def get(s,k): return keyring.get_password(s,k) or ''
def find_apikey(d, prefix):
    if isinstance(d, dict):
        for k,v in d.items():
            if k=='apiKey' and isinstance(v,str) and v.startswith(prefix): return v
            r=find_apikey(v,prefix);
            if r: return r
    elif isinstance(d, list):
        for x in d:
            r=find_apikey(x,prefix);
            if r: return r
    return ''

oc = json.load(open(r'\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\openclaw.json', encoding='utf-8-sig'))
ap = json.load(open(r'\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\agent\auth-profiles.json', encoding='utf-8-sig'))

cmp('LM key: WCM canonical vs mirror', get('sentinel-openclaw','lmstudio_api_key'), get('sentinel-watchdog','lm_api_key'), 'sentinel-watchdog')
cmp('LM key: WCM vs openclaw.json',    get('sentinel-openclaw','lmstudio_api_key'), find_apikey(oc,'sk-lm-'), 'openclaw.json')
cmp('LM key: WCM vs auth-profiles',    get('sentinel-openclaw','lmstudio_api_key'), ap.get('profiles',{}).get('lmstudio:default',{}).get('key',''), 'auth-profiles.json')
cmp('Tavily: WCM vs openclaw.json',    get('sentinel-miniapp','tavily_api_key'),    find_apikey(oc,'tvly-'), 'openclaw.json')
cmp('Agent bot: WCM vs openclaw.json', get('sentinel-miniapp','telegram_bot_token'), oc.get('channels',{}).get('telegram',{}).get('botToken',''), 'openclaw.json')
"@
}

# ── 5. WCM COVERAGE (every __WCM_*__ placeholder has a real WCM entry) ──────
if (Run "wcm") {
    Section 5 "WCM COVERAGE"
    $template = Get-Content "$RepoRoot\.env.local.template" -Raw -ErrorAction SilentlyContinue
    $placeholders = [regex]::Matches($template, '__WCM_(\w+)__') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique
    Write-Host "── $($placeholders.Count) __WCM_*__ placeholders in .env.local.template ──"
    foreach ($p in $placeholders) {
        $val = & $Py -c "import keyring; v=keyring.get_password('sentinel-miniapp','$p'); print(v[-6:] if v else 'MISSING')"
        $val = $val.Trim()
        if ($val -eq 'MISSING') { Fail ("{0,-30}  no WCM entry" -f $p) }
        else { Ok ("{0,-30}  ...$val" -f $p) }
    }
}

# ── 6. DISK / LOG HEALTH ────────────────────────────────────────────────────
if (Run "disk") {
    Section 6 "DISK + LOG HEALTH"
    Write-Host "── Free space on key volumes ──"
    foreach ($drive in @('C:','G:')) {
        try {
            $d = Get-PSDrive -Name $drive.TrimEnd(':') -ErrorAction Stop
            $freeGB = [math]::Round($d.Free/1GB, 1); $totalGB = [math]::Round(($d.Free+$d.Used)/1GB, 1)
            $pct = [math]::Round(($d.Free / ($d.Free+$d.Used)) * 100, 1)
            if ($pct -lt 10) { Fail "$drive  ${freeGB}GB free / ${totalGB}GB (${pct}%)" }
            elseif ($pct -lt 20) { Warn "$drive  ${freeGB}GB free / ${totalGB}GB (${pct}%)" }
            else { Ok "$drive  ${freeGB}GB free / ${totalGB}GB (${pct}%)" }
        } catch { Info "$drive  not found" }
    }
    Write-Host ""
    Write-Host "── Log files (size threshold: 50 MB) ──"
    $logs = @(
        "$env:USERPROFILE\metamcp-local\logs\infer_bridge.jsonl",
        "$env:USERPROFILE\metamcp-local\logs\infer_bridge_stderr.log",
        "$env:USERPROFILE\metamcp-local\logs\bridge_stderr.log"
    )
    foreach ($l in $logs) {
        if (Test-Path $l) {
            $sz = (Get-Item $l).Length / 1MB
            if ($sz -gt 50) { Warn ("{0}  {1:N1} MB" -f (Split-Path $l -Leaf), $sz) }
            else { Ok ("{0,-30}  {1:N2} MB" -f (Split-Path $l -Leaf), $sz) }
        }
    }
}

# ── 7. CONTAINER RESOURCE PRESSURE ──────────────────────────────────────────
if (Run "resources") {
    Section 7 "CONTAINER RESOURCE PRESSURE"
    docker stats --no-stream --format "{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}" 2>$null | ForEach-Object {
        $parts = $_ -split '\|'
        if ($parts.Count -ge 4) {
            $name = $parts[0]; $cpu = $parts[1].TrimEnd('%') -as [double]; $mem = $parts[2].TrimEnd('%') -as [double]
            $color = if ($cpu -gt 70 -or $mem -gt 70) { 'Yellow' } else { 'Green' }
            $tag = if ($color -eq 'Yellow') { '[warn]' } else { '[ok]  ' }
            Write-Host ("  $tag  {0,-25}  CPU {1,5}%  MEM {2,5}%  {3}" -f $name, $parts[1], $parts[2], $parts[3]) -ForegroundColor $color
        }
    }
}

# ── 8. DOCKER VOLUME ORPHANS ────────────────────────────────────────────────
if (Run "volumes") {
    Section 8 "DOCKER VOLUME ORPHANS"
    $usedVolumes = docker ps -a --format "{{.Names}}" | ForEach-Object {
        docker inspect $_ --format '{{range .Mounts}}{{.Name}} {{end}}' 2>$null
    } | Where-Object { $_ } | ForEach-Object { $_ -split ' ' } | Where-Object { $_ -and $_ -match '^[a-z0-9_-]+$' } | Select-Object -Unique
    $allVolumes = docker volume ls --format "{{.Name}}" 2>$null | Where-Object { $_ -match 'metamcp|firefly|forgejo|pia|tailscale|reminders|memory|smdl|ytdlp|onedrive|libretranslate' }
    foreach ($v in $allVolumes) {
        if ($usedVolumes -contains $v) { Ok ("in-use:  $v") }
        else { Warn ("ORPHAN:  $v  (no container references it)") }
    }
}

# ── 9. PROCESS DUPLICATION ──────────────────────────────────────────────────
if (Run "dupes") {
    Section 9 "PROCESS DUPLICATION"
    foreach ($scriptMatch in @('infer_bridge\.py', 'watchdog\\watchdog\.py', 'sentinel-miniapp-v2.*bridge\.py')) {
        $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $scriptMatch })
        $label = ($scriptMatch -replace '\\\\','\').TrimStart('.*')
        if ($procs.Count -eq 0) { Fail "$label  not running" }
        elseif ($procs.Count -eq 1) { Ok "$label  PID $($procs[0].ProcessId)" }
        else { Warn "$label  $($procs.Count) instances: $($procs.ProcessId -join ', ')" }
    }
}

# ── 10. JSON / YAML VALIDITY ────────────────────────────────────────────────
if (Run "json-yaml") {
    Section 10 "JSON + YAML VALIDITY"
    & $Py -c @"
import json, yaml, glob, os, sys
errors = []
for f in glob.glob(r'$RepoRoot\**\*.json', recursive=True):
    if 'node_modules' in f or '.git' in f or 'package-lock' in f: continue
    try:
        json.load(open(f, encoding='utf-8-sig'))
    except Exception as e:
        errors.append(('JSON', f, str(e)[:60]))
for f in glob.glob(r'$RepoRoot\**\*.yaml', recursive=True) + glob.glob(r'$RepoRoot\**\*.yml', recursive=True):
    if 'node_modules' in f or '.git' in f: continue
    try:
        yaml.safe_load(open(f, encoding='utf-8'))
    except Exception as e:
        errors.append(('YAML', f, str(e)[:60]))
if not errors:
    print('  [ok]    all JSON + YAML files parse')
else:
    for kind, f, msg in errors[:8]:
        print(f'  [FAIL]  {kind}  {f[len(r\"$RepoRoot\"):]}  -  {msg}')
    print(f'  [info] {len(errors)} total parse failures')
"@
}

# ── 11. WATCHDOG POLL FRESHNESS ─────────────────────────────────────────────
if (Run "watchdog-poll") {
    Section 11 "WATCHDOG POLL FRESHNESS"
    $wd = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'watchdog\\watchdog\.py' } | Select-Object -First 1
    if (-not $wd) { Fail "watchdog not running" }
    else {
        $uptime = (Get-Date) - $wd.CreationDate
        if ($uptime.TotalHours -gt 24) { Warn ("watchdog uptime {0:N1}h (consider restart)" -f $uptime.TotalHours) }
        else { Ok ("watchdog uptime {0:N1}h  PID {1}" -f $uptime.TotalHours, $wd.ProcessId) }
    }
}

# ── 12. BACKUP RECENCY ──────────────────────────────────────────────────────
if (Run "backups") {
    Section 12 "BACKUP RECENCY"
    foreach ($d in @('G:\AIStack-Backup\lean', 'G:\AIStack-Backup\full', "$env:USERPROFILE\Sentinel-Backups\lean", "$env:USERPROFILE\Sentinel-Backups\full")) {
        if (Test-Path $d) {
            $latest = Get-ChildItem $d -Directory | Sort-Object Name -Descending | Select-Object -First 1
            if ($latest) {
                try {
                    $latestDate = [datetime]::ParseExact($latest.Name, 'yyyy-MM-dd', $null)
                    $ageDays = ((Get-Date) - $latestDate).Days
                    if ($ageDays -gt 7) { Warn ("$d  latest=$($latest.Name)  ${ageDays}d ago") }
                    else { Ok ("$d  latest=$($latest.Name)  ${ageDays}d ago") }
                } catch { Info "$d  $($latest.Name)" }
            }
        }
    }
}

# ── 13. PS1 BOM COVERAGE ────────────────────────────────────────────────────
if (Run "ps-bom") {
    Section 13 "PS1 BOM COVERAGE"
    $bad = @()
    foreach ($f in Get-ChildItem "$RepoRoot\scripts\*.ps1") {
        $first3 = [System.IO.File]::ReadAllBytes($f.FullName)[0..2]
        if (-not ($first3[0] -eq 0xEF -and $first3[1] -eq 0xBB -and $first3[2] -eq 0xBF)) {
            $bad += $f.Name
        }
    }
    if ($bad.Count -eq 0) { Ok "all scripts/*.ps1 have UTF-8 BOM" }
    else { foreach ($f in $bad) { Warn ("no BOM: $f") } }
}

# ── 14. TAILSCALE STATE ─────────────────────────────────────────────────────
if (Run "tailscale") {
    Section 14 "TAILSCALE STATE"
    $ts = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $ts) {
        $st = & $ts status 2>&1 | Select-Object -First 1
        $online = $st -match '^\d'
        if ($online) { Ok "logged in: $($st -replace '\s+', ' ')" } else { Warn "not logged in: $st" }
    } else { Info "tailscale.exe not at expected path" }
}

# ── 15. DNS RESOLVABILITY (external deps) ───────────────────────────────────
if (Run "dns") {
    Section 15 "DNS RESOLVABILITY"
    foreach ($h in @('api.telegram.org','api.tavily.com','oauth2.googleapis.com','login.microsoftonline.com','api.github.com','one.dash.cloudflare.com')) {
        try {
            $null = Resolve-DnsName -Name $h -Type A -DnsOnly -ErrorAction Stop -QuickTimeout
            Ok $h
        } catch { Fail "$h  $($_.Exception.Message)" }
    }
}

# ── 16. METAMCP DB MIGRATIONS ───────────────────────────────────────────────
if (Run "metamcp-migrations") {
    Section 16 "METAMCP DB STATE"
    $tables = docker exec metamcp-pg psql -U metamcp_user -d metamcp_db -tA -c "SELECT count(*) FROM pg_tables WHERE schemaname='public';" 2>$null
    if ($tables) { Ok "metamcp-pg responding, $($tables.Trim()) tables" }
    else { Fail "metamcp-pg unreachable" }
    # Active MCP servers
    $servers = docker exec metamcp-pg psql -U metamcp_user -d metamcp_db -tA -c "SELECT count(*) FROM mcp_servers;" 2>$null
    if ($servers) { Info "$($servers.Trim()) MCP servers registered" }
    # DuckDuckGo paranoid recheck (we disabled it today)
    $ddg = docker exec metamcp-pg psql -U metamcp_user -d metamcp_db -tA -c "SELECT status FROM namespace_server_mappings WHERE mcp_server_uuid='ca6b2c2a-2cae-4c73-ac53-fe3ca9cb03dd';" 2>$null
    if ($ddg -match 'ACTIVE') { Warn "DuckDuckGo re-enabled in namespace (we disabled it 2026-05-11 — check why)" }
    elseif ($ddg -match 'INACTIVE') { Ok "DuckDuckGo correctly INACTIVE" }
}

# ── 17. PLAINTEXT SECRET LEAK SCAN ──────────────────────────────────────────
if (Run "leak-scan") {
    Section 17 "PLAINTEXT SECRET LEAK SCAN (tracked files)"
    Push-Location $RepoRoot
    try {
        $hits = git grep -nE 'tvly-dev-[A-Za-z0-9]{15}|sk-lm-[A-Za-z0-9:]{20}|ghp_[A-Za-z0-9]{30}|GOCSPX-[A-Za-z0-9_-]{20}|eyJhIjoi[A-Za-z0-9]{30}|AIza[A-Za-z0-9_-]{30}' 2>&1
        if ($hits) {
            Fail "tracked files contain plaintext secrets:"
            $hits | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
        } else { Ok "no plaintext token patterns in tracked files" }
    } finally { Pop-Location }
}

# ── 18. SUMMARY ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host ("═" * 68) -ForegroundColor Cyan
Write-Host "  Sanity check complete. Re-run individual sections with -Section <name>." -ForegroundColor Cyan
Write-Host "  Available: legacy parallel endpoints drift wcm disk resources volumes" -ForegroundColor Gray
Write-Host "             dupes json-yaml watchdog-poll backups ps-bom tailscale dns" -ForegroundColor Gray
Write-Host "             metamcp-migrations leak-scan" -ForegroundColor Gray
Write-Host ("═" * 68) -ForegroundColor Cyan
