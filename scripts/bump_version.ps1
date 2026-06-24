<#
.SYNOPSIS
    Bump the Sentinel Stack version, commit, tag, and push.

.PARAMETER Part
    Which part to increment: patch | minor | major

.EXAMPLE
    .\bump_version.ps1 patch    # 2.0.0 -> 2.0.1  (hotfix)
    .\bump_version.ps1 minor    # 2.0.1 -> 2.1.0  (feature release)
    .\bump_version.ps1 major    # 2.1.0 -> 3.0.0  (new generation)
#>
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("patch","minor","major")]
    [string]$Part
)

$root        = Split-Path $PSScriptRoot -Parent
$versionFile = Join-Path $root "VERSION"

# ── Read current version ──────────────────────────────────────────────────
$current = (Get-Content $versionFile -Raw).Trim()
if ($current -notmatch '^\d+\.\d+\.\d+$') {
    Write-Error "VERSION file contains invalid value: '$current'"
    exit 1
}

$parts = $current -split '\.'
[int]$major = $parts[0]
[int]$minor = $parts[1]
[int]$patch = $parts[2]

# ── Bump ──────────────────────────────────────────────────────────────────
switch ($Part) {
    "major" { $major++; $minor = 0; $patch = 0 }
    "minor" { $minor++;             $patch = 0 }
    "patch" {                       $patch++   }
}

$new = "$major.$minor.$patch"

# ── Write ─────────────────────────────────────────────────────────────────
Set-Content -Path $versionFile -Value $new -NoNewline
Write-Host "Version: $current -> $new" -ForegroundColor Cyan

# ── Git commit + tag + push ───────────────────────────────────────────────
git -C $root add VERSION
git -C $root commit -m "chore: bump to v$new"
git -C $root tag "v$new" -m "Release v$new"
git -C $root push origin master "v$new"

Write-Host ""
Write-Host "Done. Tagged and pushed: v$new" -ForegroundColor Green
