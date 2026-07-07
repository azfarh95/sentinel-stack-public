# SMDL fresh-install test orchestrator.
#
# Creates a clean WSL2 Ubuntu distro, copies the SMDL source into it,
# runs bootstrap.sh (installs docker/git/ffmpeg), builds + starts the
# container, polls the health endpoint, and tears everything down.
#
# Usage:
#   .\run-wsl2-test.ps1                  # full test, auto-cleanup
#   .\run-wsl2-test.ps1 -KeepDistro      # leave smdl-test WSL alive for inspection
#   .\run-wsl2-test.ps1 -RebuildRootfs   # re-download Ubuntu rootfs even if cached
#
# Idempotent: run repeatedly. Existing smdl-test distro is unregistered first.

[CmdletBinding()]
param(
    [switch]$KeepDistro,
    [switch]$RebuildRootfs
)

$ErrorActionPreference = "Stop"
$DistroName  = "smdl-test"
$DistroDir   = "$env:LOCALAPPDATA\WSL\$DistroName"
$RootfsCache = "$env:LOCALAPPDATA\WSL\rootfs-cache"
$RootfsTarball   = "$RootfsCache\ubuntu-24.04-rootfs.tar"
$UbuntuImage = "ubuntu:24.04"   # pulled from Docker Hub, exported to rootfs tarball

$SmdlSource = (Resolve-Path "$PSScriptRoot\..\..").Path     # the smdl/ directory
$TestDir    = "$PSScriptRoot"
$WslHomeMount = "/root/smdl-src"

function Write-Section($msg) {
    Write-Host ""
    Write-Host "── $msg ─────────────────────────────────────────────" -ForegroundColor Cyan
}

function Cleanup-Distro {
    Write-Host "Cleaning up distro $DistroName ..." -ForegroundColor DarkGray
    wsl --unregister $DistroName 2>&1 | Out-Null
}

# 1. Verify WSL2 is available
Write-Section "Preflight"
$wslList = wsl --list --quiet 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ WSL not available. Enable: wsl --install" -ForegroundColor Red
    exit 1
}
Write-Host "  WSL2: OK"

# 2. Cache the Ubuntu rootfs (via Docker Hub — more reliable than the
#    cloud-images.ubuntu.com URLs which 404 occasionally)
Write-Section "Rootfs"
New-Item -ItemType Directory -Force -Path $RootfsCache | Out-Null
if ($RebuildRootfs -or -not (Test-Path $RootfsTarball)) {
    Write-Host "  Exporting Docker Hub image $UbuntuImage to a rootfs tarball..."
    docker pull $UbuntuImage 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker pull $UbuntuImage failed — is Docker Desktop running?" }
    $cid = (docker create $UbuntuImage).Trim()
    try {
        docker export $cid -o $RootfsTarball
        if ($LASTEXITCODE -ne 0) { throw "docker export failed" }
    } finally {
        docker rm $cid 2>&1 | Out-Null
    }
    Write-Host "  rootfs cached at: $RootfsTarball"
} else {
    Write-Host "  Using cached rootfs: $RootfsTarball"
}

# 3. Tear down any prior smdl-test distro, then re-import fresh
Write-Section "Create fresh distro"
if ((wsl --list --quiet) -match $DistroName) {
    Cleanup-Distro
}
New-Item -ItemType Directory -Force -Path $DistroDir | Out-Null
wsl --import $DistroName $DistroDir $RootfsTarball --version 2
if ($LASTEXITCODE -ne 0) { throw "wsl --import failed" }
Write-Host "  $DistroName created at $DistroDir"

# 4. Copy SMDL source + bootstrap + compose into the distro
Write-Section "Copy SMDL source into distro"
# Translate Windows path to WSL /mnt path on the new distro for tar piping
$srcWslPath = (wsl -d $DistroName --exec wslpath -a $SmdlSource).Trim()
Write-Host "  SMDL source (WSL view): $srcWslPath"
wsl -d $DistroName -u root --exec bash -c "mkdir -p $WslHomeMount && cp -r $srcWslPath/. $WslHomeMount/"
if ($LASTEXITCODE -ne 0) { throw "copy failed" }

# Normalize line endings on the bootstrap script (in case Windows wrote CRLF)
wsl -d $DistroName -u root --exec bash -c "sed -i 's/\r$//' $WslHomeMount/tests/fresh-install/bootstrap.sh && chmod +x $WslHomeMount/tests/fresh-install/bootstrap.sh"

# 5. Run bootstrap (install Docker, git, ffmpeg)
Write-Section "Bootstrap (install docker + deps inside distro)"
wsl -d $DistroName -u root --exec bash $WslHomeMount/tests/fresh-install/bootstrap.sh
if ($LASTEXITCODE -ne 0) { throw "bootstrap.sh failed (exit $LASTEXITCODE)" }

# 6. Build + start SMDL via the test compose file
Write-Section "Build + start SMDL"
wsl -d $DistroName -u root --exec bash -c "cd $WslHomeMount/tests/fresh-install && docker compose -f docker-compose.test.yml up --build -d"
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

# 7. Poll the health endpoint
Write-Section "Health check"
$ok = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 2
    $body = wsl -d $DistroName -u root --exec curl -sf http://localhost:4096/health 2>$null
    if ($LASTEXITCODE -eq 0 -and $body -match '"status":"ok"') {
        Write-Host "  ✓ health OK after $($i*2)s: $body" -ForegroundColor Green
        $ok = $true
        break
    }
}
if (-not $ok) {
    Write-Host "  ✗ health check did NOT pass within 60s" -ForegroundColor Red
    Write-Host "  --- docker logs ---"
    wsl -d $DistroName -u root --exec docker logs smdl 2>&1 | Select-Object -Last 30
    if (-not $KeepDistro) { Cleanup-Distro }
    exit 1
}

# 8. Optional smoke-test: send a public YouTube URL to the bot if SMDL_BOT_TOKEN set
# (Skipped by default — needs a real bot token.)

# 9. Teardown
if ($KeepDistro) {
    Write-Section "Keeping distro alive"
    Write-Host "  Inspect: wsl -d $DistroName" -ForegroundColor Yellow
    Write-Host "  Cleanup: wsl --unregister $DistroName" -ForegroundColor Yellow
} else {
    Write-Section "Teardown"
    wsl -d $DistroName -u root --exec bash -c "cd $WslHomeMount/tests/fresh-install && docker compose -f docker-compose.test.yml down -v" 2>&1 | Out-Null
    Cleanup-Distro
}

Write-Section "PASS"
Write-Host "  Fresh-install test passed. SMDL builds + boots cleanly from a stranger's perspective." -ForegroundColor Green
