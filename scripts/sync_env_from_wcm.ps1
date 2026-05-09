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
$Python = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

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
    "smdl_bot_token",
    "github_pat",
    "onedrive_client_secret",
    "docintel_key"
)

# Load the template once
$content = Get-Content $Template -Raw

# Substitute each placeholder with its WCM value
foreach ($key in $keys) {
    $placeholder = "__WCM_${key}__"
    $value = & $Python -c "import keyring,sys; v=keyring.get_password('$Service','$key'); sys.stdout.write(v or '')" 2>$null
    if ([string]::IsNullOrEmpty($value)) {
        Write-Error "WCM has no entry for $Service/$key. Set it with: python -c `"import keyring; keyring.set_password('$Service', '$key', '<value>')`""
        exit 2
    }
    $content = $content.Replace($placeholder, $value)
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
