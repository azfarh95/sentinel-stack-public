@{
    RootModule        = 'Sentinel.Backup.psm1'
    ModuleVersion     = '0.2.0'
    GUID              = '6e8c3aaf-7c83-4f24-a3f0-2b5ba2c5b3f2'
    Author            = 'Azfar'
    Description       = 'Backup + restore a Sentinel pillar (data dirs, Docker volumes, WCM creds, scheduled tasks).'
    PowerShellVersion = '7.0'

    FunctionsToExport = @(
        'Backup-SentinelPillar',
        'Restore-SentinelPillar',
        'Get-SentinelBackupInfo',
        'Test-SentinelBackup'
    )

    # v0.2 still ships with no external module dependencies. JSON manifests
    # are first-class; YAML is supported when powershell-yaml is available.
    # PowerShell 7.0 required for System.Security.Cryptography.AesGcm.
    RequiredModules = @()

    PrivateData = @{
        PSData = @{
            Tags        = @('sentinel', 'backup', 'docker', 'wcm')
            ProjectUri  = 'https://github.com/azfarh95/sentinel-stack'
            ReleaseNotes = 'v0.2: AES-256-GCM encryption (default), real Restore-SentinelPillar, WCM layer (capture + DPAPI re-wrap on restore), -DryRun + -Force flags, pre-flight checks.'
        }
    }
}
