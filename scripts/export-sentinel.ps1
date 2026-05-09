#Requires -Version 7.0
<#
.SYNOPSIS
    Pack the entire Sentinel AI stack for migration to a new Windows machine.

.DESCRIPTION
    Produces a single output folder containing:
      - metamcp-local.zip     : the stack source (no __pycache__ / .git bloat)
      - ubuntu-24.04.tar.gz   : WSL distro export  (skipped with -SkipWsl)
      - tasks\*.xml           : Task Scheduler task exports
      - secrets\INSTRUCTIONS.txt : how to re-import secrets via store_secrets.py
      - docker-pull.ps1       : pre-warm all images on new machine
      - install-lmstudio.ps1  : one-liner LM Studio installer
      - RESTORE.md            : step-by-step restore guide

.PARAMETER OutputDir
    Destination folder for the bundle. Defaults to Desktop\sentinel-export-<date>

.PARAMETER SkipWsl
    Skip the WSL export (saves time / space when WSL is not needed or will be re-setup).

.EXAMPLE
    .\export-sentinel.ps1
    .\export-sentinel.ps1 -OutputDir D:\migration -SkipWsl
#>

[CmdletBinding()]
param(
    [string] $OutputDir = "$env:USERPROFILE\Desktop\sentinel-export-$(Get-Date -Format 'yyyy-MM-dd')",
    [switch] $SkipWsl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StackRoot   = Split-Path $PSScriptRoot -Parent   # metamcp-local\
$WslDistro   = "Ubuntu-24.04"

function Log([string]$msg, [string]$colour = "Cyan") {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor $colour
}
function Step([string]$msg) { Log ">> $msg" "Yellow" }
function Ok  ([string]$msg) { Log "   $msg" "Green"  }
function Warn ([string]$msg) { Log "   WARNING: $msg" "DarkYellow" }

# ─── 1. Create output directory ───────────────────────────────────────────────
Step "Creating output directory: $OutputDir"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
Ok "Ready"

# ─── 2. Zip metamcp-local (exclude noise) ─────────────────────────────────────
Step "Zipping metamcp-local..."
$zipPath = Join-Path $OutputDir "metamcp-local.zip"

# Collect files to include (excludes __pycache__, .git, node_modules, *.log, *.tar)
$excludePatterns = @("__pycache__", ".git", "node_modules", "*.log", "*.tar", "*.tar.gz")
$filesToZip = Get-ChildItem -Path $StackRoot -Recurse -File | Where-Object {
    $rel = $_.FullName.Substring($StackRoot.Length)
    $skip = $false
    foreach ($pat in $excludePatterns) {
        if ($rel -like "*\$pat\*" -or $rel -like "*/$pat/*" -or $_ -like $pat) {
            $skip = $true; break
        }
    }
    -not $skip
}

if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -LiteralPath $StackRoot -DestinationPath $zipPath -CompressionLevel Optimal

Ok "Zip written: $zipPath"

# ─── 3. WSL export ────────────────────────────────────────────────────────────
if (-not $SkipWsl) {
    Step "Exporting WSL distro '$WslDistro' (this can take several minutes)..."
    $wslOut = Join-Path $OutputDir "ubuntu-24.04.tar"
    wsl --export $WslDistro $wslOut
    if ($LASTEXITCODE -eq 0) {
        Ok "WSL export complete: $wslOut"
    } else {
        Warn "wsl --export returned exit code $LASTEXITCODE — file may be incomplete"
    }
} else {
    Warn "Skipping WSL export (-SkipWsl flag set)"
}

# ─── 4. Task Scheduler XML exports ────────────────────────────────────────────
Step "Exporting Task Scheduler tasks..."
$tasksDir = Join-Path $OutputDir "tasks"
New-Item -ItemType Directory -Force -Path $tasksDir | Out-Null

$taskNames = @(
    "Playwright MCP Watcher",
    "AIStack-Backup-Lean",
    "AIStack-Backup-Full",
    "Sentinel Watchdog",
    "WSL Keepalive"
)

foreach ($taskName in $taskNames) {
    try {
        $xml = schtasks /Query /TN $taskName /XML 2>&1
        if ($LASTEXITCODE -eq 0) {
            $safeName = $taskName -replace '[\\/:*?"<>|]', '_'
            $xml | Set-Content (Join-Path $tasksDir "$safeName.xml") -Encoding UTF8
            Ok "Exported: $taskName"
        } else {
            Warn "Task not found (skipped): $taskName"
        }
    } catch {
        Warn "Could not export task '$taskName': $_"
    }
}

# ─── 5. docker-pull.ps1 (pre-warm images on new machine) ─────────────────────
Step "Generating docker-pull.ps1..."

# Read image names from compose files
$composeDir = $StackRoot
$composeFiles = @(
    (Join-Path $composeDir "docker-compose.local.yml"),
    (Join-Path $composeDir "docker-compose.smdl.yml")
)

$images = @()
foreach ($cf in $composeFiles) {
    if (Test-Path $cf) {
        $images += (Select-String -Path $cf -Pattern '^\s+image:\s+(.+)' |
            ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() })
    }
}

$pullScript = @"
# Pre-warm Docker images for the Sentinel stack.
# Run this on the new machine before starting the stack.
Write-Host "Pulling Docker images..." -ForegroundColor Cyan
`$images = @(
$(($images | Sort-Object -Unique | ForEach-Object { "    '$_'" }) -join ",`n")
)
foreach (`$img in `$images) {
    Write-Host "  Pulling `$img..." -ForegroundColor Yellow
    docker pull `$img
}
Write-Host "Done." -ForegroundColor Green
"@

$pullScript | Set-Content (Join-Path $OutputDir "docker-pull.ps1") -Encoding UTF8
Ok "docker-pull.ps1 written"

# ─── 6. install-lmstudio.ps1 ──────────────────────────────────────────────────
Step "Generating install-lmstudio.ps1..."

@'
# Install LM Studio on new machine via winget.
# If winget is not available, download from https://lmstudio.ai/
Write-Host "Installing LM Studio..." -ForegroundColor Cyan
winget install --id ElementLabs.LMStudio --accept-source-agreements --accept-package-agreements
if ($LASTEXITCODE -eq 0) {
    Write-Host "LM Studio installed." -ForegroundColor Green
} else {
    Write-Host "winget failed — download manually from https://lmstudio.ai/" -ForegroundColor Yellow
}
'@ | Set-Content (Join-Path $OutputDir "install-lmstudio.ps1") -Encoding UTF8
Ok "install-lmstudio.ps1 written"

# ─── 7. Secrets instructions ──────────────────────────────────────────────────
Step "Writing secrets instructions..."
$secretsDir = Join-Path $OutputDir "secrets"
New-Item -ItemType Directory -Force -Path $secretsDir | Out-Null

@"
SECRETS SETUP
=============
Secrets are stored in Windows Credential Manager (never in files).
Run these commands on the new machine after copying the stack:

  cd C:\path\to\metamcp-local\watchdog
  py store_secrets.py bot_token    <WATCHDOG_BOT_TOKEN>
  py store_secrets.py lm_api_key  <LM_STUDIO_API_KEY_IF_ANY>
  py store_secrets.py github_pat  <GITHUB_PAT>

For the Sentinel Mini App bridge token, check sentinel-miniapp-v2\bridge.py
or the Telegram bot token stored at the same keyring service.

Also update watchdog\config.json:
  - owner_chat_id   : your Telegram user ID
  - compose_dir     : absolute path to metamcp-local on new machine
  - openclaw_config : UNC path to OpenClaw config in WSL
  - dns_watch       : your domain(s)
"@ | Set-Content (Join-Path $secretsDir "INSTRUCTIONS.txt") -Encoding UTF8
Ok "Secrets instructions written"

# ─── 8. RESTORE.md ────────────────────────────────────────────────────────────
Step "Writing RESTORE.md..."

@"
# Sentinel Stack — Restore Guide

## Prerequisites
- Windows 11
- Docker Desktop
- Python 3.11+ (py launcher)
- WSL2 enabled

## Steps

### 1. Extract stack
Unzip ``metamcp-local.zip`` to ``C:\Users\<you>\metamcp-local`` (or any path — just update config.json after).

### 2. Install LM Studio
Run ``install-lmstudio.ps1`` or download from https://lmstudio.ai/

### 3. Import WSL distro (if exported)
``````
wsl --import Ubuntu-24.04 C:\WSL\Ubuntu-24.04 ubuntu-24.04.tar
``````
Or install OpenClaw fresh: https://openclaw.ai/

### 4. Pre-warm Docker images
``````
.\docker-pull.ps1
``````

### 5. Start the stack
``````
cd C:\...\metamcp-local
docker compose -f docker-compose.local.yml -f docker-compose.smdl.yml up -d
``````

### 6. Re-import secrets
Follow ``secrets\INSTRUCTIONS.txt``

### 7. Register Task Scheduler tasks
``````
foreach (\$xml in Get-ChildItem tasks\*.xml) {
    schtasks /Create /XML \$xml.FullName /TN \$xml.BaseName /F
}
``````
Note: task paths in the XML may need updating (they contain the old username).

### 8. Install Python deps
``````
cd watchdog
pip install -r requirements.txt
``````

### 9. Verify
Start the watchdog: ``py watchdog.py``
Send /status to the bot.
"@ | Set-Content (Join-Path $OutputDir "RESTORE.md") -Encoding UTF8
Ok "RESTORE.md written"

# ─── 9. Summary ───────────────────────────────────────────────────────────────
Write-Host ""
Log "=== Export complete ===" "Green"
Write-Host ""
Write-Host "Output: $OutputDir" -ForegroundColor White
Write-Host ""
Get-ChildItem $OutputDir | Format-Table Name, @{N="Size (MB)";E={[math]::Round($_.Length/1MB,1)}} -AutoSize
