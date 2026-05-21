# scripts/generate_secrets_inventory.ps1
#
# Generates a single plaintext markdown file enumerating EVERY secret in
# the Sentinel stack - WCM entries, OAuth token files, DB-stored MCP env,
# Windows service binPaths, app-local stores - and writes it to a path
# OUTSIDE the repo so it can never be accidentally committed.
#
# Intended use: run this -> upload the output file to Vaultwarden as a
# secure note or attachment -> delete the file from disk.
#
# WARNING: the output contains PLAINTEXT secrets. Treat the path it
# writes to as you would a credentials file: don't share, don't sync to
# cloud, delete after upload.
#
# Usage:  .\scripts\generate_secrets_inventory.ps1

$ErrorActionPreference = "Stop"
$Py = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

# Output OUTSIDE the repo - in user profile root (not synced by OneDrive
# by default in fresh installs; user should still delete after upload)
$Out = Join-Path $env:USERPROFILE "secrets-inventory.md"

Write-Host ""
Write-Host "── Generating Sentinel secrets inventory ──" -ForegroundColor Cyan
Write-Host "  output: $Out" -ForegroundColor Yellow
Write-Host "  WARNING: contains PLAINTEXT secrets - upload to Vaultwarden + delete" -ForegroundColor Red
Write-Host ""

$ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
$lines = @()
$lines += "# Sentinel Secrets Inventory"
$lines += ""
$lines += "Generated: ``$ts`` on ``$($env:COMPUTERNAME)``"
$lines += ""
$lines += "**This file contains plaintext secrets. Upload to Vaultwarden as a secure note, then delete from disk.**"
$lines += ""

# ── 1. Windows Credential Manager ────────────────────────────────────────────
$lines += "## 1. Windows Credential Manager (live read path)"
$lines += ""
$lines += "WCM is the canonical store; ``sync_env_from_wcm.ps1`` materializes these into ``.env.local`` for Docker."
$lines += ""

$wcmDump = & $Py -c @"
import keyring
import keyring.backends.Windows
keyring.set_keyring(keyring.backends.Windows.WinVaultKeyring())
# (service, key) pairs we know about
entries = [
    ('sentinel-miniapp', 'telegram_bot_token'),
    ('sentinel-miniapp', 'smdl_bot_token'),
    ('sentinel-miniapp', 'github_pat'),
    ('sentinel-miniapp', 'onedrive_client_secret'),
    ('sentinel-miniapp', 'docintel_key'),
    ('sentinel-miniapp', 'vaultwarden_admin_token'),
    ('sentinel-miniapp', 'telethon_api_id'),
    ('sentinel-miniapp', 'telethon_api_hash'),
    ('sentinel-miniapp', 'telethon_session'),
    ('sentinel-miniapp', 'smdl_share_secret'),
    ('sentinel-miniapp', 'better_auth_secret'),
    ('sentinel-miniapp', 'tavily_api_key'),
    ('sentinel-miniapp', 'google_oauth_client_secret'),
    ('sentinel-miniapp', 'mini_app_secret'),
    ('sentinel-miniapp', 'totp_secret'),
    ('sentinel-miniapp', 'cloudflared_token'),
    ('sentinel-miniapp', 'pia_user'),
    ('sentinel-miniapp', 'pia_password'),
    ('sentinel-miniapp', 'pia_region'),
    ('sentinel-miniapp', 'pia_dedicated_ip_token'),
    ('sentinel-miniapp', 'ts_authkey_pia_exit'),
    ('sentinel-watchdog', 'bot_token'),
    ('sentinel-openclaw', 'lmstudio_api_key'),
    ('sentinel-smdl', 'bot_token'),
]
for svc, key in entries:
    try:
        v = keyring.get_password(svc, key)
    except Exception as e:
        v = f'<ERROR: {e}>'
    if v is None:
        print(f'  {svc}/{key}|<not set>')
    else:
        print(f'  {svc}/{key}|{v}')
"@

$wcmDump -split "`n" | Where-Object { $_.Trim() -ne "" } | ForEach-Object {
    $parts = $_ -split '\|', 2
    $name = $parts[0].Trim()
    $val  = if ($parts.Count -gt 1) { $parts[1] } else { '<missing>' }
    $lines += "- **$name**"
    $lines += "  ``````"
    $lines += "  $val"
    $lines += "  ``````"
}

# ── 2. OpenClaw config files ────────────────────────────────────────────────
$lines += ""
$lines += "## 2. OpenClaw config files"
$lines += ""

$lines += "### ``/home/azfar/.openclaw/openclaw.json`` (WSL-native) -> channels.telegram.botToken"
$openclawJson = wsl -d Ubuntu-24.04 -u azfar --exec bash -c "python3 -c `"import json; cfg=json.load(open('/home/azfar/.openclaw/openclaw.json', encoding='utf-8-sig')); print(cfg.get('channels',{}).get('telegram',{}).get('botToken','<missing>'))`""
$lines += "``````"
$lines += $openclawJson.Trim()
$lines += "``````"
$lines += ""

$lines += "### ``/home/azfar/.openclaw/agents/main/agent/auth-profiles.json`` (WSL-native) -> profiles.lmstudio:default.key"
$authProfilesJson = wsl -d Ubuntu-24.04 -u azfar --exec bash -c "python3 -c `"import json; cfg=json.load(open('/home/azfar/.openclaw/agents/main/agent/auth-profiles.json', encoding='utf-8-sig')); print(cfg.get('profiles',{}).get('lmstudio:default',{}).get('key','<missing>'))`""
$lines += "``````"
$lines += $authProfilesJson.Trim()
$lines += "``````"
$lines += ""

# ── 3. OAuth token files ─────────────────────────────────────────────────────
$lines += "## 3. OAuth token files (in-place rewritten by libraries)"
$lines += ""

$googleCred = "C:\Users\azfar\metamcp-local\google-workspace-mcp\data\credentials.json"
$googleTok  = "C:\Users\azfar\metamcp-local\google-workspace-mcp\data\token.json"
$onedriveTok = "C:\Users\azfar\metamcp-local\onedrive-mcp\data\token.json"

foreach ($f in @($googleCred, $googleTok, $onedriveTok)) {
    if (Test-Path $f) {
        $rel = $f.Replace("C:\Users\azfar\metamcp-local\", "")
        $lines += "### ``$rel``"
        $lines += "``````json"
        $lines += (Get-Content $f -Raw).Trim()
        $lines += "``````"
        $lines += ""
    }
}

# ── 4. MetaMCP-DB-stored MCP env (Tavily) ───────────────────────────────────
$lines += "## 4. MetaMCP Postgres (mcp_servers.env)"
$lines += ""
$lines += "Stored inline in DB rows; updated via UI or ``rotate_tavily.ps1`` (which calls UPDATE on the JSON column)."
$lines += ""

$mcpEnv = docker exec metamcp-pg psql -U metamcp_user -d metamcp_db -tA -c "SELECT name || ' | ' || env::text FROM mcp_servers WHERE env IS NOT NULL AND env::text != '{}';" 2>$null
if ($mcpEnv) {
    $lines += "``````"
    $lines += ($mcpEnv -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { "  $_" }) -join "`n"
    $lines += "``````"
} else {
    $lines += "_(metamcp-pg unreachable - run docker ps to verify)_"
}
$lines += ""

# ── 5. Cloudflare Tunnel service ────────────────────────────────────────────
$lines += "## 5. Cloudflare Tunnel connector token (Windows service binPath)"
$lines += ""
$cfSvc = Get-CimInstance Win32_Service -Filter "Name='cloudflared'" -ErrorAction SilentlyContinue
if ($cfSvc) {
    $lines += "``````"
    $lines += "PathName: $($cfSvc.PathName)"
    $lines += "``````"
} else {
    $lines += "_(cloudflared service not found)_"
}
$lines += ""

# ── 6. Telethon session (lives inside SMDL container) ───────────────────────
$lines += "## 6. Telethon session"
$lines += ""
$lines += "WCM ``sentinel-miniapp/telethon_session`` is the canonical source (see section 1)."
$lines += "The container materializes a ``.session`` SQLite file on first run, but that's regenerated from the WCM value."
$lines += ""

# ── 7. LM Studio app (opaque) ───────────────────────────────────────────────
$lines += "## 7. LM Studio app API keys (opaque, app-internal)"
$lines += ""
$lines += "LM Studio Desktop maintains its own key list. The ACTIVE key for OpenClaw is mirrored at:"
$lines += "- WCM ``sentinel-openclaw/lmstudio_api_key`` (section 1)"
$lines += "- ``auth-profiles.json`` (section 2)"
$lines += ""
$lines += "Whatever you see in the LM Studio UI 'API Keys' panel should match those two."
$lines += ""

# ── 8. Postgres credentials (low-rotation) ──────────────────────────────────
$lines += "## 8. MetaMCP Postgres credentials"
$lines += ""
$lines += "Hardcoded in ``.env.local.template`` (internal docker network only)."
$lines += ""
$lines += "``````"
$lines += "POSTGRES_USER=metamcp_user"
$lines += "POSTGRES_PASSWORD=m3t4mcp"
$lines += "POSTGRES_DB=metamcp_db"
$lines += "``````"
$lines += ""

# ── 9. Bootstrap user (MetaMCP web admin) ───────────────────────────────────
$lines += "## 9. MetaMCP bootstrap admin"
$lines += ""
$lines += "Hardcoded in ``.env.local.template`` - login at http://localhost:12008"
$lines += ""
$lines += "``````"
$lines += "BOOTSTRAP_USER_EMAIL=admin@localhost"
$lines += "BOOTSTRAP_USER_PASSWORD=eKdmXrvrvT0xs^2A"
$lines += "``````"
$lines += ""

# ── Write + summary ─────────────────────────────────────────────────────────
$lines -join "`n" | Set-Content -Path $Out -Encoding UTF8 -NoNewline

$size = (Get-Item $Out).Length
Write-Host "[ok] inventory written: $Out ($size bytes)" -ForegroundColor Green
Write-Host ""
Write-Host "── Next steps ──" -ForegroundColor Cyan
Write-Host "  1. Open Vaultwarden at http://127.0.0.1:8085" -ForegroundColor Yellow
Write-Host "  2. New Item -> Secure Note, title: 'Sentinel Secrets Inventory ($($ts.Substring(0,10)))'"
Write-Host "  3. Paste contents of: $Out" -ForegroundColor Yellow
Write-Host "  4. Save in folder 'Sentinel' (create if needed)"
Write-Host "  5. DELETE the file:  Remove-Item '$Out'" -ForegroundColor Red
Write-Host ""
Write-Host "  Optional: re-run this script monthly to refresh the Vaultwarden note." -ForegroundColor DarkGray
