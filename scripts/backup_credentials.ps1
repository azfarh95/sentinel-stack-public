# backup_credentials.ps1
# Exports all Sentinel-related Windows Credential Manager entries to an
# encrypted JSON file (DPAPI — current Windows user only can decrypt).
#
# Usage:
#   .\scripts\backup_credentials.ps1
#   .\scripts\backup_credentials.ps1 -OutputDir D:\backups
#
# To restore on the SAME Windows user account:
#   .\scripts\restore_credentials.ps1 -InputFile <path>
#
# To migrate to a different machine, decrypt to a clear-text JSON file
# (use only on a machine you fully trust), copy securely, then re-import.

[CmdletBinding()]
param(
    [string]$OutputDir = "$env:USERPROFILE\Desktop\sentinel-credentials-backup-$(Get-Date -Format 'yyyy-MM-dd-HHmmss')"
)

$ErrorActionPreference = "Stop"
$Python = "C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

# PowerShell 7+ doesn't auto-load System.Security.Cryptography.ProtectedData
Add-Type -AssemblyName System.Security

# All known Sentinel-related WCM entries
$entries = @(
    @{ service = "sentinel-miniapp";  user = "telegram_bot_token"     },
    @{ service = "sentinel-miniapp";  user = "mini_app_secret"        },
    @{ service = "sentinel-miniapp";  user = "totp_secret"            },
    @{ service = "sentinel-miniapp";  user = "openrouter_api_key"     },
    @{ service = "sentinel-miniapp";  user = "metamcp_bearer_token"   },
    @{ service = "sentinel-miniapp";  user = "better_auth_secret"     },
    @{ service = "sentinel-miniapp";  user = "smdl_bot_token"         },
    @{ service = "sentinel-miniapp";  user = "github_pat"             },
    @{ service = "sentinel-miniapp";  user = "onedrive_client_secret" },
    @{ service = "sentinel-miniapp";  user = "docintel_key"           },
    @{ service = "sentinel-watchdog"; user = "bot_token"              },
    @{ service = "sentinel-watchdog"; user = "lm_api_key"             }
)

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

Write-Host "Reading WCM entries..."
$result = @()
$missing = @()
foreach ($e in $entries) {
    $v = & $Python -c "import keyring,sys; v=keyring.get_password('$($e.service)','$($e.user)'); sys.stdout.write(v or '')" 2>$null
    if ([string]::IsNullOrEmpty($v)) {
        $missing += "$($e.service)/$($e.user)"
        continue
    }
    $result += @{
        service = $e.service
        user    = $e.user
        value   = $v
    }
}

# Serialise + DPAPI encrypt (current Windows user only)
$json = $result | ConvertTo-Json -Depth 3 -Compress
$bytes = [Text.Encoding]::UTF8.GetBytes($json)
$encrypted = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, "CurrentUser")

$outFile = Join-Path $OutputDir "wcm-secrets.dpapi"
[IO.File]::WriteAllBytes($outFile, $encrypted)

# Also write a small instructions file
@"
Sentinel WCM credentials backup
Created: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Windows user: $env:USERNAME
Entries: $($result.Count)
Missing: $($missing.Count)

File: wcm-secrets.dpapi
Encryption: Windows DPAPI (CurrentUser scope)
- Can ONLY be decrypted by the same Windows user account on the same machine.
- For cross-machine restore, decrypt on this machine first to a temp clear-text
  file, then copy securely (or import into a password manager like Bitwarden).

Restore (same machine, same user):
  .\scripts\restore_credentials.ps1 -InputFile $outFile

Migration to Vaultwarden / Bitwarden:
  See V3 roadmap — credential migration phase planned. For now, decrypt to
  a temporary JSON, use 'bw create item' or the Bitwarden web vault to
  import each entry, then delete the temp JSON.

Missing entries (not in WCM at backup time):
$(if ($missing.Count -eq 0) { '  (none)' } else { $missing | ForEach-Object { "  - $_" } | Out-String })
"@ | Set-Content -Path (Join-Path $OutputDir "README.txt") -Encoding UTF8

Write-Host ""
Write-Host "[backup] $($result.Count) WCM entries backed up" -ForegroundColor Green
Write-Host "[backup] Output: $OutputDir" -ForegroundColor Green
if ($missing.Count -gt 0) {
    Write-Host "[backup] Missing $($missing.Count) entry/entries (skipped)" -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host "         - $_" -ForegroundColor Yellow }
}
Write-Host ""
Write-Host "Files in backup:"
Get-ChildItem $OutputDir | Format-Table Name, Length, LastWriteTime
