<#
.SYNOPSIS
    Store the GitHub PAT in Windows Credential Manager and push it as a
    GitHub Actions repository secret so the auto-version workflow can use it.

.PARAMETER Force
    Re-prompt for the token even if one is already stored in Credential Manager.

.EXAMPLE
    .\setup_secrets.ps1          # first run — prompts, saves, pushes
    .\setup_secrets.ps1 -Force   # rotate / update the token
#>
param([switch]$Force)

$VAULT_RESOURCE = "sentinel-stack"
$VAULT_USERNAME = "github-pat"

# ── Windows Credential Manager (PasswordVault) ────────────────────────────────
$null = [Windows.Security.Credentials.PasswordVault,
         Windows.Security.Credentials,
         ContentType=WindowsRuntime]
$vault = New-Object Windows.Security.Credentials.PasswordVault

function Get-VaultToken {
    try {
        $c = $vault.Retrieve($VAULT_RESOURCE, $VAULT_USERNAME)
        $c.RetrievePassword()
        return $c.Password
    } catch {
        return $null
    }
}

function Save-VaultToken([string]$token) {
    try {
        $old = $vault.Retrieve($VAULT_RESOURCE, $VAULT_USERNAME)
        $vault.Remove($old)
    } catch {}
    $vault.Add(
        [Windows.Security.Credentials.PasswordCredential]::new(
            $VAULT_RESOURCE, $VAULT_USERNAME, $token
        )
    )
    Write-Host "  Saved to Windows Credential Manager." -ForegroundColor Green
}

# ── Determine token ───────────────────────────────────────────────────────────
$pat = $null

if (-not $Force) {
    $pat = Get-VaultToken
    if ($pat) {
        Write-Host "Using stored PAT from Windows Credential Manager." -ForegroundColor Cyan
    }
}

if (-not $pat) {
    $secure = Read-Host "Enter GitHub PAT (classic, repo + workflow scopes)" -AsSecureString
    $pat    = [System.Net.NetworkCredential]::new("", $secure).Password
    if (-not $pat) { Write-Error "No token entered. Aborting."; exit 1 }
    Save-VaultToken $pat
}

# ── Resolve repo slug from remote URL ─────────────────────────────────────────
$root   = Split-Path $PSScriptRoot -Parent
$remote = git -C $root remote get-url origin 2>$null
if (-not $remote) { Write-Error "No git remote 'origin' found."; exit 1 }

# Handle both HTTPS and SSH remote formats
$slug = $remote `
    -replace '^https://github\.com/', '' `
    -replace '^git@github\.com:', ''  `
    -replace '\.git$', ''

Write-Host "Pushing PAT secret to $slug ..." -ForegroundColor Cyan
$result = gh secret set PAT --body $pat --repo $slug 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "gh secret set failed: $result"
    exit 1
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Credential Manager key : $VAULT_RESOURCE / $VAULT_USERNAME"
Write-Host "  GitHub Actions secret  : PAT  (repo: $slug)"
Write-Host ""
Write-Host "The auto-version workflow will now use your PAT so version bump" -ForegroundColor DarkGray
Write-Host "commits also trigger docker-publish." -ForegroundColor DarkGray
