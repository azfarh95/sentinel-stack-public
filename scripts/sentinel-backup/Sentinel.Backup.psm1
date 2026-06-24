# Sentinel.Backup — module entry point
#
# Dot-sources every .ps1 under Public/ and Private/, then exports only the
# Public functions. Per-pillar manifests live in <pillar-repo>/sentinel-backup.{yaml,json}
# and are resolved at command-invocation time.
#
# Spec: ../../docs/sentinel-backup-module-v1.md

$ErrorActionPreference = 'Stop'

# Module-scoped constants — surface via $script:* so functions can reach them.
$script:SentBakMagic    = [byte[]]@(0x53,0x45,0x4E,0x54,0x42,0x41,0x4B,0x00)  # "SENTBAK\0"
$script:SentBakVersion  = 'v1'
$script:SentBakHeaderSz = 256

# Dot-source everything under Private/ first (so Public can call helpers),
# then Public.
foreach ($scope in @('Private', 'Public')) {
    $dir = Join-Path $PSScriptRoot $scope
    if (Test-Path $dir) {
        Get-ChildItem -Path $dir -Filter '*.ps1' -File | ForEach-Object {
            . $_.FullName
        }
    }
}

# Public surface.
Export-ModuleMember -Function @(
    'Backup-SentinelPillar',
    'Restore-SentinelPillar',
    'Get-SentinelBackupInfo',
    'Test-SentinelBackup'
)
