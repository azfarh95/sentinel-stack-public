# restore_credentials.ps1
# Decrypts a wcm-secrets.dpapi backup and re-imports each entry into Windows
# Credential Manager. Only works on the same Windows user account that
# originally created the backup (DPAPI CurrentUser scope).

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$InputFile
)

$ErrorActionPreference = "Stop"
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $Python) { Write-Error "No Python found on PATH"; exit 1 }

Add-Type -AssemblyName System.Security

if (-not (Test-Path $InputFile)) {
    Write-Error "Backup file not found: $InputFile"
    exit 1
}

Write-Host "Reading + decrypting $InputFile..."
$encrypted = [IO.File]::ReadAllBytes($InputFile)
try {
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $null, "CurrentUser")
} catch {
    Write-Error "Decryption failed - this backup was created by a different Windows user account or on a different machine. DPAPI CurrentUser scope cannot cross those boundaries."
    exit 2
}
$json = [Text.Encoding]::UTF8.GetString($bytes)
$entries = $json | ConvertFrom-Json

Write-Host "Restoring $($entries.Count) entries to Windows Credential Manager..."
$ok = 0
$fail = 0
foreach ($e in $entries) {
    try {
        & $Python -c "import keyring; keyring.set_password('$($e.service)', '$($e.user)', '$($e.value)')"
        Write-Host "  $($e.service)/$($e.user) -> ok" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "  $($e.service)/$($e.user) -> FAIL: $_" -ForegroundColor Red
        $fail++
    }
}

Write-Host ""
Write-Host "[restore] $ok succeeded, $fail failed" -ForegroundColor $(if ($fail -eq 0) { "Green" } else { "Yellow" })
