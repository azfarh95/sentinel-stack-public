function Restore-SentinelPillar {
    <#
    .SYNOPSIS
        Restore a Sentinel pillar from a .bak file.
    .DESCRIPTION
        v0.2: real mutation. Decrypts the bak (if encrypted), extracts, runs
        pre-flight checks, then walks each layer:
          data    → untar; existing dirs renamed to .pre-restore-<ts> unless -Force
          volumes → docker volume create + extract; existing non-empty volume
                    errors unless -Force (drops + recreates)
          wcm     → CredWrite (DPAPI re-wrap on this host's user)
          tasks   → v0.3 (still stubbed)
          cloud   → checklist-only per spec §9
        Compose services in the bak's manifest are stopped before data/volumes
        restore and restarted in finally{} — never leave the stack down.

    .PARAMETER InFile
        .bak file to restore.
    .PARAMETER Passphrase
        Required if the .bak is encrypted; prompts if missing.
    .PARAMETER DryRun
        Print what would happen; do not mutate.
    .PARAMETER Force
        Overwrite existing data dirs / volumes without renaming-aside.
    .PARAMETER Layers
        Restore a subset (default: data, volumes, wcm).

    .EXAMPLE
        Restore-SentinelPillar -InFile shopping.bak
    .EXAMPLE
        Restore-SentinelPillar -InFile finance.bak -Passphrase (Read-Host -AsSecureString) -DryRun
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]   $InFile,
        [securestring]                    $Passphrase,
        [switch]                          $DryRun,
        [switch]                          $Force,
        [string[]] $Layers = @('data','volumes','wcm')
    )
    $ErrorActionPreference = 'Stop'
    $started = Get-Date

    # ── Inspect header; prompt for passphrase if needed ─────────────
    $info = Read-SentBakHeader -Path $InFile
    if ($info.Header.encrypted -and -not $Passphrase) {
        $Passphrase = Read-Host -AsSecureString "Passphrase for $InFile"
    }

    # ── Extract body to temp dir ────────────────────────────────────
    $extractDir = Join-Path $env:TEMP "sentinel-bak-restore-$PID-$(Get-Random)"
    if ($info.Header.encrypted) {
        Extract-SentBakBody -InFile $InFile -DestDir $extractDir -Passphrase $Passphrase | Out-Null
    } else {
        Extract-SentBakBody -InFile $InFile -DestDir $extractDir | Out-Null
    }

    $manifestPath = Join-Path $extractDir 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "manifest.json missing in extracted .bak body — corrupt or wrong format"
    }
    $bakManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

    Write-Host ""
    if ($DryRun) {
        Write-Host "=== DRY-RUN RESTORE PLAN ===" -ForegroundColor Cyan
    } else {
        Write-Host "=== RESTORING PILLAR ===" -ForegroundColor Cyan
    }
    Write-Host "Pillar:     $($bakManifest.pillar)"
    Write-Host "CapturedAt: $($bakManifest.captured_at)"
    Write-Host "CapturedBy: $($bakManifest.captured_by)"
    Write-Host "Encrypted:  $($info.Header.encrypted)"
    Write-Host ""

    # ── Pre-flight ──────────────────────────────────────────────────
    if (-not $DryRun) {
        $needsDocker = ($Layers -contains 'volumes') -and `
                       ($bakManifest.layers.PSObject.Properties.Name -contains 'volumes')
        $needsWcm    = ($Layers -contains 'wcm') -and `
                       ($bakManifest.layers.PSObject.Properties.Name -contains 'wcm')
        $preflight = Test-RestorePreflight -NeedsDocker:$needsDocker -NeedsWcm:$needsWcm
        if ($preflight.Count -gt 0) {
            Write-Host "Pre-flight failures:" -ForegroundColor Red
            $preflight | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
            throw "Pre-flight checks failed; restore aborted (no mutation done)."
        }
    }

    # ── Stop compose services for data/volumes restore safety ───────
    $composeFile  = $null
    $composeSvcs  = @()
    $servicesStopped = $false
    if ($bakManifest.PSObject.Properties.Name -contains 'compose' -and $bakManifest.compose) {
        if ($bakManifest.compose.file)     { $composeFile = $bakManifest.compose.file }
        if ($bakManifest.compose.services) { $composeSvcs = @($bakManifest.compose.services) }
    }
    $needsStop = (-not $DryRun) -and $composeFile -and $composeSvcs.Count -gt 0 -and (
        ($Layers -contains 'data') -or ($Layers -contains 'volumes')
    )

    $summary = [ordered]@{}
    try {
        if ($needsStop -and (Test-Path -LiteralPath $composeFile)) {
            Write-Host "Stopping $($composeSvcs.Count) compose service(s) for restore safety…" -ForegroundColor Yellow
            try { Invoke-ComposeStop -ComposeFile $composeFile -Services $composeSvcs; $servicesStopped = $true } catch {
                Write-Warning "compose stop failed (continuing anyway): $_"
            }
            Start-Sleep -Seconds 2
        }

        foreach ($layerName in $Layers) {
            if (-not ($bakManifest.layers.PSObject.Properties.Name -contains $layerName)) {
                Write-Host "  $layerName : (not in this .bak — skipping)" -ForegroundColor DarkGray
                continue
            }
            $layer = $bakManifest.layers.$layerName
            switch ($layerName) {
                'data' {
                    Write-Host "Restoring data layer…" -ForegroundColor Yellow
                    $summary['data'] = Invoke-RestoreData -DataItems @($layer.items) -StageDir $extractDir -DryRun:$DryRun -Force:$Force
                    Write-Host ("  data:    Restored={0} Failed={1}" -f $summary['data'].Restored, $summary['data'].Failed)
                }
                'volumes' {
                    Write-Host "Restoring volumes layer…" -ForegroundColor Yellow
                    $summary['volumes'] = Invoke-RestoreVolumes -VolumeItems @($layer.items) -StageDir $extractDir -DryRun:$DryRun -Force:$Force
                    Write-Host ("  volumes: Restored={0} Failed={1}" -f $summary['volumes'].Restored, $summary['volumes'].Failed)
                }
                'wcm' {
                    Write-Host "Restoring wcm layer…" -ForegroundColor Yellow
                    $summary['wcm'] = Invoke-RestoreWcm -StageDir $extractDir -DryRun:$DryRun
                    Write-Host ("  wcm:     Restored={0} Failed={1}" -f $summary['wcm'].Restored, $summary['wcm'].Failed)
                }
                'tasks' {
                    Write-Host "  tasks  : v0.3 will handle Task Scheduler entry restoration" -ForegroundColor DarkGray
                    $summary['tasks'] = [PSCustomObject]@{ Layer='tasks'; Restored=0; Skipped=0; Failed=0; Items=@() }
                }
                'cloud' {
                    Write-Host "  cloud  : v0.4 will emit a CF-state checklist" -ForegroundColor DarkGray
                    $summary['cloud'] = [PSCustomObject]@{ Layer='cloud'; Restored=0; Skipped=0; Failed=0; Items=@() }
                }
            }
        }
    } finally {
        if ($servicesStopped) {
            Write-Host "Restarting compose service(s)…" -ForegroundColor Yellow
            try { Invoke-ComposeStart -ComposeFile $composeFile -Services $composeSvcs } catch {
                Write-Warning "compose restart failed (services may need manual start): $_"
            }
        }
        Remove-Item -Recurse -Force -LiteralPath $extractDir -ErrorAction SilentlyContinue
    }

    $totalRestored = ($summary.Values | Measure-Object -Property Restored -Sum).Sum
    $totalFailed   = ($summary.Values | Measure-Object -Property Failed   -Sum).Sum

    Write-Host ""
    if ($DryRun) {
        Write-Host "✓ Dry-run complete (no mutation)." -ForegroundColor Green
    } elseif ($totalFailed -gt 0) {
        Write-Host ("✗ Restore finished with {0} failure(s)." -f $totalFailed) -ForegroundColor Red
    } else {
        Write-Host "✓ Restore complete." -ForegroundColor Green
    }
    Write-Host ("  Total restored: {0}" -f $totalRestored)
    Write-Host ("  Total failed:   {0}" -f $totalFailed)
    Write-Host ("  Elapsed:        {0:N1}s" -f ((Get-Date) - $started).TotalSeconds)

    return [PSCustomObject]@{
        DryRun        = [bool]$DryRun
        Pillar        = $bakManifest.pillar
        TotalRestored = $totalRestored
        TotalFailed   = $totalFailed
        Layers        = $summary
    }
}
