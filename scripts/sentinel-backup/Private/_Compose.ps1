# Docker compose helpers — start + stop a pillar's services around a capture.


function Invoke-ComposeStop {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]   $ComposeFile,
        [Parameter(Mandatory)] [string[]] $Services
    )
    if (-not (Test-Path -LiteralPath $ComposeFile)) {
        throw "Compose file not found: $ComposeFile"
    }
    # `docker compose stop` (not down) — keeps containers + volumes intact,
    # just halts running processes. Restart restores state instantly.
    & docker compose -f $ComposeFile stop @Services 2>&1 | ForEach-Object {
        Write-Verbose "docker: $_"
    }
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose stop failed (exit $LASTEXITCODE)"
    }
}


function Invoke-ComposeStart {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]   $ComposeFile,
        [Parameter(Mandatory)] [string[]] $Services
    )
    if (-not (Test-Path -LiteralPath $ComposeFile)) {
        throw "Compose file not found: $ComposeFile"
    }
    & docker compose -f $ComposeFile start @Services 2>&1 | ForEach-Object {
        Write-Verbose "docker: $_"
    }
    if ($LASTEXITCODE -ne 0) {
        # start failure isn't always fatal (some services rely on others that
        # we didn't restart). Caller decides.
        Write-Warning "docker compose start returned $LASTEXITCODE (services may need manual restart)"
    }
}
