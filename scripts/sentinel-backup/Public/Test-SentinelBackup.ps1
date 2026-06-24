function Test-SentinelBackup {
    <#
    .SYNOPSIS
        Decrypt + extract a .bak into a temp dir, validate its manifest.json,
        hash each layer's contents. Doesn't mutate the host.
    .DESCRIPTION
        Use this to verify a 2-week-old backup is still readable + intact.
        Useful as a periodic safety check or before relying on a .bak for
        actual restore.
    .EXAMPLE
        Test-SentinelBackup -InFile C:\backups\finance-2026-05-27.bak
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $InFile
    )
    $ErrorActionPreference = 'Stop'

    $info = Read-SentBakHeader -Path $InFile
    if ($info.Header.encrypted) {
        Write-Warning @"
Test-SentinelBackup does not decrypt encrypted .bak yet (scoped for v0.3).
For an encrypted .bak, use one of:
  - Get-SentinelBackupInfo -InFile <bak>          # header-only, no decryption needed
  - Restore-SentinelPillar -InFile <bak> -DryRun  # full decrypt + extract, prints plan
"@
        return $null
    }

    $extractDir = Join-Path $env:TEMP "sentinel-bak-test-$PID-$(Get-Random)"
    try {
        Extract-SentBakBody -InFile $InFile -DestDir $extractDir | Out-Null

        $manifestPath = Join-Path $extractDir 'manifest.json'
        if (-not (Test-Path -LiteralPath $manifestPath)) {
            throw "manifest.json missing in .bak body"
        }
        $bakManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

        Write-Host "✓ .bak is readable" -ForegroundColor Green
        Write-Host ("  Pillar:      {0}" -f $bakManifest.pillar)
        Write-Host ("  CapturedAt:  {0}" -f $bakManifest.captured_at)
        Write-Host ("  CapturedBy:  {0}" -f $bakManifest.captured_by)
        foreach ($k in $bakManifest.layers.PSObject.Properties.Name) {
            $l = $bakManifest.layers.$k
            Write-Host ("  Layer {0,-10} {1,3} items, {2:N0} bytes" -f $k, $l.count, $l.size_bytes)
        }

        return [PSCustomObject]@{
            OK           = $true
            Pillar       = $bakManifest.pillar
            CapturedAt   = $bakManifest.captured_at
            Layers       = $bakManifest.layers
            ExtractedTo  = $extractDir
        }
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $extractDir -ErrorAction SilentlyContinue
    }
}
