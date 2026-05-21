# sync_env_from_wcm.ps1
# Reads .env.local.template, substitutes secrets from Windows Credential Manager,
# writes .env.local. Run this before any docker compose command.
#
# WCM is the canonical source of truth for the 6 secrets below.
# Template lives in git; .env.local is generated and gitignored.
#
# To rotate a secret: update WCM via `python -c "import keyring; keyring.set_password('sentinel-miniapp', '<key>', '<value>')"`
# Then re-run this script.

param(
    [string]$Service = "sentinel-miniapp"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Template = Join-Path $Root ".env.local.template"
$Output = Join-Path $Root ".env.local"
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $Python) { Write-Error "No Python found on PATH"; exit 1 }

if (-not (Test-Path $Template)) {
    Write-Error "Template not found: $Template"
    exit 1
}
if (-not (Test-Path $Python)) {
    $Python = "python"  # fall back to PATH lookup
}

$keys = @(
    "better_auth_secret",
    "telegram_bot_token",
    "testbot_token",
    "smdl_bot_token",
    "github_pat",
    "onedrive_client_secret",
    "docintel_key",
    "vaultwarden_admin_token",
    "owner_auth_token",
    "telethon_api_id",
    "telethon_api_hash",
    "telethon_session",
    "smdl_share_secret",
    # Sentinel Finance auth (portfolio-mcp)
    "google_client_id",
    "google_client_secret",
    # Firefly III Personal Access Token (read by portfolio-mcp + scripts)
    "firefly_pat",
    # Moralis Web3 API key (read by portfolio-mcp for wallet snapshots)
    "moralis_api_key",
    # Etherscan v2 unified API key (eth/bsc/base/arbitrum/polygon/avalanche/optimism)
    "etherscan_api_key",
    # Cronos EVM Explorer key (custom v1 API, post-Cronoscan-deprecation)
    "cronos_explorer_api_key",
    # Wise API token (read-only) — used by portfolio-mcp's Wise sync
    "wise_api_token",
    # Sentinel Finance agent bearer token — read by portfolio-mcp /api/agent/*
    # (consumer: Sentinel AI / @YourSentinelBot). Rotate by re-storing in WCM
    # under sentinel-miniapp/sentinel_finance_agent_token, then re-run this script.
    "sentinel_finance_agent_token",
    # VPN exit-node (profile: vpn) — populated via rotate_pia_creds.ps1
    "pia_user",
    "pia_password",
    "pia_region",
    "pia_dedicated_ip_token",
    "ts_authkey_pia_exit"
)

# Load the template once
$content = Get-Content $Template -Raw

# Substitute each placeholder with its WCM value.
#
# Docker Compose interpolation gotcha: values written into .env.local get
# scanned for $VAR / ${VAR} patterns when compose substitutes them into the
# compose config. A literal `$` in the value (common in argon2id hashes like
# `$argon2id$v=19$m=...$<salt>$<hash>`) gets parsed as a variable reference
# and silently replaced with empty string — corrupting the secret.
#
# Fix: escape every `$` in the value as `$$` before writing. Compose un-
# escapes `$$` back to `$` during interpolation. Hit this 2026-05-11 with
# VAULTWARDEN_ADMIN_TOKEN — the salt segment `$FuC04qI3` was mangled to
# blank, breaking /admin login.
foreach ($key in $keys) {
    $placeholder = "__WCM_${key}__"
    $value = & $Python -c "import keyring,sys; v=keyring.get_password('$Service','$key'); sys.stdout.write(v or '')" 2>$null
    if ([string]::IsNullOrEmpty($value)) {
        Write-Error "WCM has no entry for $Service/$key. Set it with: python -c `"import keyring; keyring.set_password('$Service', '$key', '<value>')`""
        exit 2
    }
    # Escape $ for docker-compose interpolation safety
    $escaped = $value.Replace('$', '$$')
    $content = $content.Replace($placeholder, $escaped)
}

# Verify no placeholders remain
if ($content -match "__WCM_[a-z_]+__") {
    Write-Error "Unsubstituted placeholders remain in output: $($Matches[0])"
    exit 3
}

# Backup current .env.local before overwriting (safety net)
if (Test-Path $Output) {
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item $Output "$Output.backup-$ts" -ErrorAction SilentlyContinue
}

# Atomic write: temp file + rename
$Tmp = "$Output.tmp"
Set-Content -Path $Tmp -Value $content -NoNewline
Move-Item -Path $Tmp -Destination $Output -Force

Write-Host "[sync] $Output regenerated from WCM ($($keys.Count) secrets)"
