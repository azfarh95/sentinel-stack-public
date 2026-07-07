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
    # SMDL license-authority HMAC key — signs every issued license key's bearer
    # secret. MUST stay stable forever (rotation invalidates all issued keys).
    "license_signing_secret",
    # SMDL -> License Registry service token (X-Sentinel-Service-Token). Mirror
    # of issued/revoked keys to watchdog v2 /api/v2/licenses/*.
    "license_registry_token",
    # /internal/reload-env shared token (#27 fanout) — written by sentinel-secrets,
    # verified by every consumer (watchdog v2, smdl, bridge, finance, shared brain).
    "internal_reload_token",
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
    "ts_authkey_pia_exit",
    # Coinbox arcade (public Credits economy, ../coinbox-credits, D-018). Without
    # these the PUBLIC arcade silently falls back to the insecure compose dev
    # defaults and the DB-password drift crash-loops coinbox-credits. The GPU
    # broker token lives under a DIFFERENT WCM service -> $crossServiceKeys below.
    "coinbox_db_password",
    "coinbox_session_secret",
    "coinbox_bot_token",
    # Previously hand-maintained keys (manifest-declared) now wired in so re-sync
    # stops dropping them. sentinel-miniapp service -> here; non-default services
    # -> $crossServiceKeys below.
    "homeassistant_llm_token",
    "headscale_preauth_key"
)

# Keys whose WCM entry lives under a NON-default service. Maps the template
# placeholder suffix -> @{ service; user }. Substituted after the main loop.
$crossServiceKeys = @{
    # COINBOX_GPU_BROKER_TOKEN mirrors the watchdog gpu-broker-client service token.
    "coinbox_gpu_broker_token" = @{ Service = "sentinel-watchdog"; User = "gpu-broker-client" }
    # CF_DNS01_TOKEN (caddy-tailnet ACME DNS-01) + HOME_APP_JWT_SECRET (sentinel-home).
    "cf_dns01_token"      = @{ Service = "caddy-tailnet"; User = "cf_dns01_token" }
    "home_app_jwt_secret" = @{ Service = "sentinel-home"; User = "app_jwt_secret" }
    # POSTGRES_PASSWORD (metamcp_db, shared by the gateway + OpenClaw brain). Was a
    # stale hardcoded literal in the template; pull the real 28-char value from WCM
    # so a sync never again clobbers it and breaks the brain bot/bridge on restart.
    "postgres_password"   = @{ Service = "sentinel-metamcp"; User = "postgres_password" }
}

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

# Cross-service keys: same __WCM_<suffix>__ placeholder, but fetched from a
# non-default WCM service/username (e.g. the watchdog gpu-broker-client token).
foreach ($suffix in $crossServiceKeys.Keys) {
    $map = $crossServiceKeys[$suffix]
    $placeholder = "__WCM_${suffix}__"
    $value = & $Python -c "import keyring,sys; v=keyring.get_password('$($map.Service)','$($map.User)'); sys.stdout.write(v or '')" 2>$null
    if ([string]::IsNullOrEmpty($value)) {
        Write-Error "WCM has no entry for $($map.Service)/$($map.User) (placeholder $placeholder)."
        exit 2
    }
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
