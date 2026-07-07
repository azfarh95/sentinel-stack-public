# Layer-specific capture + restore primitives.
#
# Each layer is independent: data tars host-side dirs, volumes tars
# Docker-managed volumes via a one-shot alpine container, wcm walks
# the Windows credential store. Returned objects are layer summaries
# that feed manifest.json inside the bak.


# ── data layer ──────────────────────────────────────────────────────


function Invoke-CaptureData {
    <#
    .SYNOPSIS
        Tar each bind-mount directory listed in the manifest into the staging
        dir under data/<in_bak>.tar.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [array]  $DataItems,        # from manifest.data[]
        [Parameter(Mandatory)] [string] $ManifestDir,
        [Parameter(Mandatory)] [string] $StageDir          # bak staging temp
    )

    $outDir = Join-Path $StageDir 'data'
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    $captured = @()
    $totalBytes = 0

    foreach ($d in $DataItems) {
        $src = Resolve-ManifestPath -RelativePath $d.path -ManifestDir $ManifestDir
        if (-not (Test-Path -LiteralPath $src)) {
            if ($d.optional) {
                Write-Verbose "data: skipping optional missing $src"
                continue
            }
            throw "data layer: required path missing: $src"
        }

        $inBak = $d.in_bak
        if (-not $inBak) { $inBak = Split-Path $src -Leaf }
        $tarPath = Join-Path $outDir "$inBak.tar"

        # `tar -cf <tar> -C <parent> <basename>` packages the directory itself
        # so on restore the structure is preserved.
        $parent = Split-Path $src -Parent
        $base   = Split-Path $src -Leaf
        & tar.exe -cf $tarPath -C $parent $base
        if ($LASTEXITCODE -ne 0) {
            throw "tar failed on data layer for $src (exit $LASTEXITCODE)"
        }

        $size = (Get-Item -LiteralPath $tarPath).Length
        $totalBytes += $size
        $captured += [PSCustomObject]@{
            source = $src
            in_bak = "$inBak.tar"
            bytes  = $size
        }
        Write-Verbose ("data: {0} → {1} ({2} bytes)" -f $src, $inBak, $size)
    }

    return [PSCustomObject]@{
        Layer      = 'data'
        Items      = $captured
        Count      = $captured.Count
        TotalBytes = $totalBytes
    }
}


# ── volumes layer ────────────────────────────────────────────────────


function Invoke-CaptureVolumes {
    <#
    .SYNOPSIS
        For each named Docker volume, spawn a throwaway alpine container that
        tars the volume's contents to a host-mounted output dir.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [array]  $VolumeItems,      # from manifest.volumes[]
        [Parameter(Mandatory)] [string] $StageDir
    )

    $outDir = Join-Path $StageDir 'volumes'
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    $captured = @()
    $totalBytes = 0

    foreach ($v in $VolumeItems) {
        $vol = $v.name
        if (-not $vol) { continue }

        # Verify volume exists; if not, skip with a warning.
        & docker volume inspect $vol 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "volumes: '$vol' not found on this host — skipping"
            continue
        }

        $outFile = Join-Path $outDir "$vol.tar.gz"
        # Convert host path to Docker-friendly form. The alpine container mounts
        # the volume read-only at /source, plus the output dir at /out.
        $outDirAbs = (Resolve-Path -LiteralPath $outDir).Path

        # docker run -v <vol>:/source:ro -v <outDir>:/out alpine sh -c "tar -czf /out/<vol>.tar.gz -C /source ."
        $cmd = "tar -czf /out/$vol.tar.gz -C /source ."
        & docker run --rm `
            -v "${vol}:/source:ro" `
            -v "${outDirAbs}:/out" `
            alpine sh -c $cmd 2>&1 | ForEach-Object { Write-Verbose "docker: $_" }
        if ($LASTEXITCODE -ne 0) {
            throw "volume capture failed for '$vol' (exit $LASTEXITCODE)"
        }

        $size = (Get-Item -LiteralPath $outFile).Length
        $totalBytes += $size
        $captured += [PSCustomObject]@{
            volume = $vol
            in_bak = "$vol.tar.gz"
            bytes  = $size
        }
        Write-Verbose ("volumes: {0} → {1} bytes" -f $vol, $size)
    }

    return [PSCustomObject]@{
        Layer      = 'volumes'
        Items      = $captured
        Count      = $captured.Count
        TotalBytes = $totalBytes
    }
}


# ── data + volumes restore (v0.2: real mutation) ───────────────────


function Invoke-RestoreData {
    <#
    .SYNOPSIS
        Untar each data-layer tarball from the extracted stage dir back to its
        captured source path. Existing dirs are renamed to .pre-restore-<ts>
        unless -Force is set (in which case they're overwritten in place).

        v0.2 NOTE: 'source' paths in the bak manifest are absolute paths from
        capture time. Same-host restore works as-is; true cross-host path
        rewriting lands in a future phase.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [array]  $DataItems,         # from bak-manifest.layers.data.items
        [Parameter(Mandatory)] [string] $StageDir,          # where the bak was extracted
        [switch] $DryRun,
        [switch] $Force
    )

    $dataDir = Join-Path $StageDir 'data'
    $restored = @()
    $renamed  = @()
    $skipped  = @()
    $failed   = @()

    foreach ($item in $DataItems) {
        $source  = $item.source
        $tarName = $item.in_bak
        $tarPath = Join-Path $dataDir $tarName

        if (-not (Test-Path -LiteralPath $tarPath)) {
            $failed += [PSCustomObject]@{ source = $source; reason = "tarball missing in bak: $tarName" }
            continue
        }

        # The tar was created with `-C <parent> <basename>` so it contains
        # the directory itself; we extract into the parent dir.
        $parent = Split-Path $source -Parent
        $base   = Split-Path $source -Leaf

        if ($DryRun) {
            Write-Host ("  [dry-run] would restore '{0}' (tar={1})" -f $source, $tarName)
            continue
        }

        if (Test-Path -LiteralPath $source) {
            if ($Force) {
                Write-Verbose "data: -Force overwriting existing $source"
            } else {
                $ts = (Get-Date).ToString('yyyyMMdd-HHmmss')
                $bak = "$source.pre-restore-$ts"
                Write-Host ("  data: renaming existing {0} → {1}" -f $source, (Split-Path $bak -Leaf)) -ForegroundColor Yellow
                try {
                    Move-Item -LiteralPath $source -Destination $bak -Force
                    $renamed += [PSCustomObject]@{ from = $source; to = $bak }
                } catch {
                    $failed += [PSCustomObject]@{ source = $source; reason = "rename failed: $($_.Exception.Message)" }
                    continue
                }
            }
        }

        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        & tar.exe -xf $tarPath -C $parent
        if ($LASTEXITCODE -ne 0) {
            $failed += [PSCustomObject]@{ source = $source; reason = "tar.exe exit $LASTEXITCODE" }
            continue
        }
        $restored += $source
        Write-Verbose ("data: restored {0}" -f $source)
    }

    return [PSCustomObject]@{
        Layer    = 'data'
        Restored = $restored.Count
        Skipped  = if ($DryRun) { $DataItems.Count } else { $skipped.Count }
        Renamed  = $renamed
        Failed   = $failed.Count
        Errors   = $failed
        Items    = $restored
    }
}


function Invoke-RestoreVolumes {
    <#
    .SYNOPSIS
        For each volume tarball in the bak, ensure the named Docker volume
        exists (empty), then extract the tarball into it via a throwaway
        alpine container. If the volume exists with content, error unless
        -Force (in which case drop + recreate).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [array]  $VolumeItems,       # from bak-manifest.layers.volumes.items
        [Parameter(Mandatory)] [string] $StageDir,
        [switch] $DryRun,
        [switch] $Force
    )

    $volDir = Join-Path $StageDir 'volumes'
    $restored = @()
    $failed   = @()

    foreach ($item in $VolumeItems) {
        $vol     = $item.volume
        $tarName = $item.in_bak
        $tarPath = Join-Path $volDir $tarName

        if (-not (Test-Path -LiteralPath $tarPath)) {
            $failed += [PSCustomObject]@{ volume = $vol; reason = "tarball missing in bak: $tarName" }
            continue
        }

        if ($DryRun) {
            Write-Host ("  [dry-run] would restore volume '{0}' (tar={1})" -f $vol, $tarName)
            continue
        }

        # Check whether the volume already exists.
        & docker volume inspect $vol 2>&1 | Out-Null
        $exists = ($LASTEXITCODE -eq 0)
        if ($exists) {
            # If it has content, refuse unless -Force.
            $hasContent = $false
            try {
                $listOut = & docker run --rm -v "${vol}:/v:ro" alpine sh -c 'ls -A /v | head -1' 2>$null
                if ($LASTEXITCODE -eq 0 -and $listOut) { $hasContent = $true }
            } catch {}

            if ($hasContent -and -not $Force) {
                $failed += [PSCustomObject]@{ volume = $vol; reason = 'volume already has content; rerun with -Force to overwrite' }
                Write-Warning "volumes: '$vol' has content; skipping (use -Force to overwrite)"
                continue
            }

            if ($Force) {
                Write-Host ("  volumes: -Force, dropping existing '{0}'" -f $vol) -ForegroundColor Yellow
                & docker volume rm $vol 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    $failed += [PSCustomObject]@{ volume = $vol; reason = "docker volume rm failed (exit $LASTEXITCODE) — is something still using it?" }
                    continue
                }
            }
        }

        # Create (or recreate) the volume + extract the tarball into it.
        & docker volume create $vol 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $failed += [PSCustomObject]@{ volume = $vol; reason = "docker volume create failed (exit $LASTEXITCODE)" }
            continue
        }

        $volDirAbs = (Resolve-Path -LiteralPath $volDir).Path
        $cmd = "tar -xzf /src/$tarName -C /dest"
        & docker run --rm `
            -v "${vol}:/dest" `
            -v "${volDirAbs}:/src:ro" `
            alpine sh -c $cmd 2>&1 | ForEach-Object { Write-Verbose "docker: $_" }
        if ($LASTEXITCODE -ne 0) {
            $failed += [PSCustomObject]@{ volume = $vol; reason = "tar extract into volume failed (exit $LASTEXITCODE)" }
            continue
        }
        $restored += $vol
        Write-Verbose ("volumes: restored {0}" -f $vol)
    }

    return [PSCustomObject]@{
        Layer    = 'volumes'
        Restored = $restored.Count
        Skipped  = if ($DryRun) { $VolumeItems.Count } else { 0 }
        Failed   = $failed.Count
        Errors   = $failed
        Items    = $restored
    }
}


function Test-RestorePreflight {
    <#
    .SYNOPSIS
        Pre-flight checks before mutation: docker daemon, schtasks service,
        WCM API reachable. Returns a list of failed checks; empty = all good.
    #>
    [CmdletBinding()]
    param(
        [switch] $NeedsDocker,
        [switch] $NeedsWcm
    )
    $fail = @()

    if ($NeedsDocker) {
        & docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $fail += "Docker daemon not reachable (`docker info` returned $LASTEXITCODE). Start Docker Desktop."
        }
    }
    if ($NeedsWcm) {
        try {
            [void][SentinelWcm]::List()
        } catch {
            $fail += "Windows Credential Manager API not reachable: $($_.Exception.Message)"
        }
    }
    return $fail
}
