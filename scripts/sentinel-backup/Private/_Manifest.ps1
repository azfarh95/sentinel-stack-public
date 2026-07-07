# Per-pillar manifest loading + path resolution.
#
# Manifest schema is spec'd in §5 of docs/sentinel-backup-module-v1.md.
# v0.1 accepts JSON OR YAML; JSON is preferred since it needs no extra module.
# A pillar's manifest lives at <pillar-repo>/sentinel-backup.{yaml,json}.


function Find-PillarManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Name,
        [string] $SearchRoot = 'C:\Users\azfar'
    )

    # Map common short-names to repo dirs. Falls back to "sentinel-<name>".
    $candidates = @(
        "$SearchRoot\sentinel-$Name",
        "$SearchRoot\$Name",
        "$SearchRoot\metamcp-local\sentinel-$Name"    # post-nest-reorg homes
    )
    foreach ($dir in $candidates) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        foreach ($ext in @('json', 'yaml', 'yml')) {
            $p = Join-Path $dir "sentinel-backup.$ext"
            if (Test-Path -LiteralPath $p) {
                return [PSCustomObject]@{
                    PillarDir    = (Resolve-Path -LiteralPath $dir).Path
                    ManifestPath = (Resolve-Path -LiteralPath $p).Path
                    Format       = $ext
                }
            }
        }
    }
    throw "No sentinel-backup.{json,yaml,yml} found for pillar '$Name' under $SearchRoot. Tried: $($candidates -join ', ')"
}


function Read-PillarManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Format
    )

    $raw = Get-Content -LiteralPath $Path -Raw

    switch ($Format) {
        'json' {
            return ($raw | ConvertFrom-Json)
        }
        { $_ -in 'yaml','yml' } {
            # Try the powershell-yaml module if installed; else error with hint.
            $hasYaml = Get-Module -ListAvailable -Name powershell-yaml
            if (-not $hasYaml) {
                throw @"
Manifest is YAML but the 'powershell-yaml' module isn't installed.
Either:
  • Install-Module powershell-yaml -Scope CurrentUser   (one-time)
  • Or write the manifest as sentinel-backup.json instead
"@
            }
            Import-Module powershell-yaml -ErrorAction Stop
            return (ConvertFrom-Yaml $raw)
        }
    }
}


function Resolve-ManifestPath {
    <#
    .SYNOPSIS
        Resolve a manifest-relative path to an absolute path on disk.
    #>
    param(
        [Parameter(Mandatory)] [string] $RelativePath,
        [Parameter(Mandatory)] [string] $ManifestDir
    )
    if ([System.IO.Path]::IsPathRooted($RelativePath)) {
        return $RelativePath
    }
    return (Join-Path $ManifestDir $RelativePath | Resolve-Path -ErrorAction SilentlyContinue).Path `
        ?? (Join-Path $ManifestDir $RelativePath)
}
