function Backup-SentinelPillar {
    <#
    .SYNOPSIS
        Capture a Sentinel pillar's state into a single .bak file.
    .DESCRIPTION
        v0.2 implementation. Captures data + volumes + wcm layers per the
        pillar's sentinel-backup manifest. tasks/cloud remain stubbed for
        v0.3+. Default output is AES-256-GCM encrypted; use -Plain to disable.
    .PARAMETER Name
        Pillar name (e.g. 'finance'). Locates manifest via Find-PillarManifest.
    .PARAMETER OutFile
        Destination .bak path. Parent dir is created if missing.
    .PARAMETER Passphrase
        SecureString used to derive the AES-256 key. If omitted (and -Plain not
        set), prompts interactively. Forgotten passphrase = unrecoverable backup.
    .PARAMETER Plain
        Skip encryption entirely. Only for backups landing in already-protected
        storage (BitLocker, WCM-vaulted dirs). Emits a warning.
    .PARAMETER SkipVolumes
        Skip Docker volume capture (much faster — useful for incremental).
    .PARAMETER SkipWcm
        Skip the WCM credentials layer (faster, but a partial backup).
    .PARAMETER StopServices
        Stop the pillar's docker compose services before capturing. Default $true.
    .EXAMPLE
        Backup-SentinelPillar -Name finance -OutFile C:\backups\finance-2026-05-27.bak
    .EXAMPLE
        $pp = Read-Host -AsSecureString
        Backup-SentinelPillar -Name shopping -OutFile shopping.bak -Passphrase $pp
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $OutFile,
        [securestring] $Passphrase,
        [switch] $Plain,
        [switch] $SkipVolumes,
        [switch] $SkipWcm,
        [bool]   $StopServices = $true
    )

    $ErrorActionPreference = 'Stop'
    $started  = Get-Date

    # ── Resolve passphrase up front so we fail fast if the user cancels ─
    if (-not $Plain) {
        if (-not $Passphrase) {
            $Passphrase = Read-Host -AsSecureString "Passphrase for new .bak (will be required to restore)"
            $confirm    = Read-Host -AsSecureString "Confirm passphrase"
            if (-not (Test-SecureStringEqual -A $Passphrase -B $confirm)) {
                throw "Passphrases do not match. Aborting."
            }
        }
    } else {
        Write-Warning "-Plain selected: backup will NOT be encrypted. Ensure the destination is in protected storage."
    }

    # ── Find + read manifest ────────────────────────────────────────
    $manifestRef = Find-PillarManifest -Name $Name
    Write-Host "Manifest: $($manifestRef.ManifestPath)" -ForegroundColor Cyan
    $manifest    = Read-PillarManifest -Path $manifestRef.ManifestPath -Format $manifestRef.Format
    $manifestDir = Split-Path $manifestRef.ManifestPath -Parent

    if ($manifest.pillar -ne $Name) {
        Write-Warning "Manifest 'pillar' field is '$($manifest.pillar)' but you passed -Name '$Name'. Using manifest value."
    }

    # ── Staging dir for layer outputs ───────────────────────────────
    $stageDir = Join-Path $env:TEMP "sentinel-backup-$PID-$(Get-Random)"
    New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
    Write-Verbose "Staging: $stageDir"

    $composeFile = $null
    $composeSvcs = @()
    if ($manifest.compose) {
        $composeFile = Resolve-ManifestPath -RelativePath $manifest.compose.file -ManifestDir $manifestDir
        $composeSvcs = @($manifest.compose.services)
    }

    $layerSummary = @{}
    $error_during_capture = $null

    try {
        # ── Stop services (if requested) ────────────────────────────
        if ($StopServices -and $composeFile -and $composeSvcs.Count -gt 0) {
            Write-Host "Stopping $($composeSvcs.Count) compose service(s)…" -ForegroundColor Yellow
            Invoke-ComposeStop -ComposeFile $composeFile -Services $composeSvcs
            Start-Sleep -Seconds 3  # let in-flight writes flush
        }

        # ── Layer: data ─────────────────────────────────────────────
        if ($manifest.data -and @($manifest.data).Count -gt 0) {
            Write-Host "Capturing data layer ($(@($manifest.data).Count) item(s))…" -ForegroundColor Yellow
            $layerSummary['data'] = Invoke-CaptureData -DataItems @($manifest.data) -ManifestDir $manifestDir -StageDir $stageDir
            Write-Host ("  data: {0} item(s), {1:N0} bytes" -f $layerSummary['data'].Count, $layerSummary['data'].TotalBytes)
        }

        # ── Layer: volumes ──────────────────────────────────────────
        if (-not $SkipVolumes -and $manifest.volumes -and @($manifest.volumes).Count -gt 0) {
            Write-Host "Capturing volumes layer ($(@($manifest.volumes).Count) volume(s))…" -ForegroundColor Yellow
            $layerSummary['volumes'] = Invoke-CaptureVolumes -VolumeItems @($manifest.volumes) -StageDir $stageDir
            Write-Host ("  volumes: {0} item(s), {1:N0} bytes" -f $layerSummary['volumes'].Count, $layerSummary['volumes'].TotalBytes)
        }

        # ── Layer: wcm (v0.2) ───────────────────────────────────────
        if (-not $SkipWcm -and $manifest.wcm -and $manifest.wcm.patterns -and @($manifest.wcm.patterns).Count -gt 0) {
            Write-Host "Capturing wcm layer ($(@($manifest.wcm.patterns).Count) pattern(s))…" -ForegroundColor Yellow
            $layerSummary['wcm'] = Invoke-CaptureWcm -Patterns @($manifest.wcm.patterns) -StageDir $stageDir
            Write-Host ("  wcm: {0} item(s), {1:N0} bytes" -f $layerSummary['wcm'].Count, $layerSummary['wcm'].TotalBytes)
        }

        # ── Layers stubbed for later phases ─────────────────────────
        # tasks: v0.3
        # cloud: v0.4

        # ── manifest.json inside the bak ────────────────────────────
        $bakManifest = [ordered]@{
            version          = 'v1'
            pillar           = "$($manifest.pillar)"
            captured_at      = $started.ToUniversalTime().ToString('o')
            captured_by      = "$env:COMPUTERNAME / $env:USERNAME"
            manifest_version = 1
            layers           = @{}
        }
        # Carry compose info so restore knows which services to stop/start.
        if ($composeFile) {
            $bakManifest['compose'] = @{
                file     = $composeFile
                services = $composeSvcs
            }
        }
        foreach ($k in $layerSummary.Keys) {
            $s = $layerSummary[$k]
            $bakManifest['layers'][$k] = @{
                count       = $s.Count
                size_bytes  = $s.TotalBytes
                items       = $s.Items
            }
        }
        ($bakManifest | ConvertTo-Json -Depth 10) |
            Out-File -Encoding UTF8 -LiteralPath (Join-Path $stageDir 'manifest.json')

    } catch {
        $error_during_capture = $_
        Write-Warning "Capture failed mid-way: $_"
    } finally {
        # Always try to restart services — never leave them down.
        if ($StopServices -and $composeFile -and $composeSvcs.Count -gt 0) {
            Write-Host "Restarting compose service(s)…" -ForegroundColor Yellow
            try { Invoke-ComposeStart -ComposeFile $composeFile -Services $composeSvcs } catch {
                Write-Warning "Compose restart failed (services may need manual start): $_"
            }
        }
    }

    if ($error_during_capture) {
        Write-Host "Staging dir preserved for forensics: $stageDir" -ForegroundColor Red
        throw $error_during_capture
    }

    # ── Tar + gzip the stage, then prepend SENTBAK header ───────────
    Write-Host "Packaging $stageDir into $OutFile…" -ForegroundColor Yellow
    $tmpTarGz = Join-Path $env:TEMP "sentinel-bak-body-$PID-$(Get-Random).tar.gz"
    & tar.exe -czf $tmpTarGz -C $stageDir .
    if ($LASTEXITCODE -ne 0) { throw "tar.exe failed packaging body (exit $LASTEXITCODE)" }

    $parent = Split-Path $OutFile -Parent
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }

    $bakHeader = @{
        pillar      = "$($manifest.pillar)"
        captured_at = $started.ToUniversalTime().ToString('o')
        encrypted   = (-not $Plain)
        host        = $env:COMPUTERNAME
        gz          = $true
    }
    $outStream = [System.IO.File]::Create($OutFile)
    try {
        Write-SentBakHeader -Stream $outStream -Header $bakHeader
        if ($Plain) {
            $bodyStream = [System.IO.File]::OpenRead($tmpTarGz)
            try { $bodyStream.CopyTo($outStream) } finally { $bodyStream.Dispose() }
        } else {
            Write-SentBakEncryptedBody -OutStream $outStream -PlainBodyPath $tmpTarGz -Passphrase $Passphrase
        }
    } finally {
        $outStream.Dispose()
    }
    Remove-Item -LiteralPath $tmpTarGz -Force -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -LiteralPath $stageDir -ErrorAction SilentlyContinue

    $finalSize = (Get-Item -LiteralPath $OutFile).Length
    $elapsed   = (Get-Date) - $started

    Write-Host ""
    Write-Host "✓ Backup complete." -ForegroundColor Green
    Write-Host ("  Pillar:    {0}" -f $manifest.pillar)
    Write-Host ("  Out file:  {0}" -f (Resolve-Path -LiteralPath $OutFile).Path)
    Write-Host ("  Size:      {0:N0} bytes" -f $finalSize)
    Write-Host ("  Elapsed:   {0:N1}s" -f $elapsed.TotalSeconds)
    Write-Host ("  Layers:    {0}" -f ($layerSummary.Keys -join ', '))
    Write-Host ("  Encrypted: {0}" -f (-not $Plain))

    return [PSCustomObject]@{
        OutFile     = (Resolve-Path -LiteralPath $OutFile).Path
        SizeBytes   = $finalSize
        CapturedAt  = $bakHeader.captured_at
        Pillar      = $manifest.pillar
        Layers      = $layerSummary
        ElapsedSec  = [Math]::Round($elapsed.TotalSeconds, 1)
    }
}
