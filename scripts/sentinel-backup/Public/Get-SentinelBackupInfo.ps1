function Get-SentinelBackupInfo {
    <#
    .SYNOPSIS
        Read ONLY the .bak header — no body decode, no decryption.
    .DESCRIPTION
        Cheap (one file open, 272 bytes read). Returns pillar name, captured
        timestamp, host, encryption status, version. Useful for "is this
        backup recent?" and "which pillar is this?" without spending the
        decrypt + tar.gz extraction cost.
    .EXAMPLE
        Get-SentinelBackupInfo -InFile C:\backups\finance-2026-05-27.bak
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $InFile
    )
    $info = Read-SentBakHeader -Path $InFile
    return [PSCustomObject]@{
        Path        = $info.Path
        Version     = $info.Version
        Pillar      = $info.Header.pillar
        CapturedAt  = $info.Header.captured_at
        Host        = $info.Header.host
        Encrypted   = [bool]$info.Header.encrypted
        Gzipped     = [bool]$info.Header.gz
        FileSize    = $info.FileSize
        BodySize    = $info.FileSize - $info.BodyOffset
    }
}
