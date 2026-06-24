# Windows Credential Manager (WCM) capture + restore.
#
# Goes via P/Invoke to advapi32.dll because cmdkey /list doesn't expose the
# credential blob (only target+user). The Credential API does.
#
# Dual-format matching: cmdkey writes targets as "<service>:<user>" while
# python-keyring writes "<user>@<service>". Both coexist on this host.
# Patterns in the manifest are matched against BOTH forms so the user
# doesn't have to know which convention each cred ended up using.
# See feedback_wcm_keyring_target_format + feedback_wcm_dual_target_naming.


if (-not ('SentinelWcm' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class SentinelWcm {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct CREDENTIAL {
        public int Flags;
        public int Type;
        [MarshalAs(UnmanagedType.LPWStr)] public string TargetName;
        [MarshalAs(UnmanagedType.LPWStr)] public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public int CredentialBlobSize;
        public IntPtr CredentialBlob;
        public int Persist;
        public int AttributeCount;
        public IntPtr Attributes;
        [MarshalAs(UnmanagedType.LPWStr)] public string TargetAlias;
        [MarshalAs(UnmanagedType.LPWStr)] public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredRead(string target, int type, int reservedFlag, out IntPtr credentialPtr);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool CredFree([In] IntPtr credentialPtr);

    [DllImport("advapi32.dll", EntryPoint = "CredEnumerateW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredEnumerate(string filter, int flag, out int count, out IntPtr credentialPtrs);

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite([In] ref CREDENTIAL credential, int flags);

    private const int CRED_TYPE_GENERIC = 1;
    private const int CRED_PERSIST_LOCAL_MACHINE = 2;

    public class CredItem {
        public string Target;
        public string User;
        public byte[] BlobBytes;
        public int Type;
        public int Persist;
    }

    public static string[] List() {
        IntPtr arr;
        int count;
        if (!CredEnumerate(null, 0, out count, out arr)) return new string[0];
        try {
            var result = new string[count];
            for (int i = 0; i < count; i++) {
                IntPtr credPtr = Marshal.ReadIntPtr(arr, i * IntPtr.Size);
                var c = Marshal.PtrToStructure<CREDENTIAL>(credPtr);
                result[i] = c.TargetName;
            }
            return result;
        } finally {
            CredFree(arr);
        }
    }

    public static CredItem Read(string target) {
        IntPtr ptr;
        if (!CredRead(target, CRED_TYPE_GENERIC, 0, out ptr)) return null;
        try {
            var c = Marshal.PtrToStructure<CREDENTIAL>(ptr);
            byte[] blob = null;
            if (c.CredentialBlobSize > 0 && c.CredentialBlob != IntPtr.Zero) {
                blob = new byte[c.CredentialBlobSize];
                Marshal.Copy(c.CredentialBlob, blob, 0, c.CredentialBlobSize);
            }
            return new CredItem {
                Target = c.TargetName,
                User = c.UserName,
                BlobBytes = blob,
                Type = c.Type,
                Persist = c.Persist
            };
        } finally {
            CredFree(ptr);
        }
    }

    public static bool Write(string target, string user, byte[] blob, int persist) {
        var c = new CREDENTIAL();
        c.Type = CRED_TYPE_GENERIC;
        c.TargetName = target;
        c.UserName = user;
        c.CredentialBlobSize = blob.Length;
        c.CredentialBlob = Marshal.AllocHGlobal(blob.Length);
        c.Persist = (persist == 0 ? CRED_PERSIST_LOCAL_MACHINE : persist);
        c.AttributeCount = 0;
        c.Attributes = IntPtr.Zero;
        try {
            Marshal.Copy(blob, 0, c.CredentialBlob, blob.Length);
            return CredWrite(ref c, 0);
        } finally {
            if (c.CredentialBlob != IntPtr.Zero) Marshal.FreeHGlobal(c.CredentialBlob);
        }
    }
}
'@
}


function ConvertTo-WcmDualForm {
    <#
    .SYNOPSIS
        Given a pattern like 'wise_api_token@sentinel-miniapp', return both that
        AND the cmdkey-style 'sentinel-miniapp:wise_api_token'. Both forms are
        used on this host (memory: feedback_wcm_dual_target_naming).
        Patterns with no '@' or ':' are returned as-is (single-element array).
    #>
    param([Parameter(Mandatory)] [string] $Pattern)
    if ($Pattern -match '^(.+)@(.+)$') {
        $user    = $Matches[1]
        $service = $Matches[2]
        return @($Pattern, "${service}:${user}")
    }
    if ($Pattern -match '^(.+):(.+)$') {
        $service = $Matches[1]
        $user    = $Matches[2]
        return @($Pattern, "${user}@${service}")
    }
    return @($Pattern)
}


function Invoke-CaptureWcm {
    <#
    .SYNOPSIS
        For each pattern in manifest.wcm.patterns, find matching WCM targets
        (in both <user>@<service> and <service>:<user> forms) and capture
        their credential blobs into wcm/credentials.json under the stage dir.
    .DESCRIPTION
        v0.2 NOTE: the blob is stored as base64 plaintext inside the .bak.
        The outer AES-256-GCM is the only encryption layer. Design doc §10
        called for DPAPI-double-wrapping but that breaks cross-host restore,
        so v0.2 keeps it simple. Cross-host works; outer encryption protects
        the .bak at rest.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string[]] $Patterns,
        [Parameter(Mandatory)] [string]   $StageDir
    )

    $outDir = Join-Path $StageDir 'wcm'
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    $allTargets = [SentinelWcm]::List()
    $expanded   = @()
    foreach ($p in $Patterns) {
        $expanded += (ConvertTo-WcmDualForm -Pattern $p)
    }

    # Glob match each expanded pattern against the full target list.
    $matchedTargets = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($exp in $expanded) {
        foreach ($t in $allTargets) {
            if ($t -like $exp -and -not $matchedTargets.Contains($t)) {
                [void]$matchedTargets.Add($t)
            }
        }
    }

    $captured = @()
    $totalBytes = 0
    foreach ($t in $matchedTargets) {
        $c = [SentinelWcm]::Read($t)
        if (-not $c) {
            Write-Warning "wcm: CredRead returned null for '$t' — skipping"
            continue
        }
        $blobB64 = if ($c.BlobBytes) { [Convert]::ToBase64String($c.BlobBytes) } else { '' }
        $captured += [PSCustomObject]@{
            target     = $c.Target
            user       = $c.User
            blob_b64   = $blobB64
            blob_bytes = if ($c.BlobBytes) { $c.BlobBytes.Length } else { 0 }
            persist    = $c.Persist
            cred_type  = $c.Type
        }
        $totalBytes += $blobB64.Length
        Write-Verbose ("wcm: captured {0} (user={1}, {2} blob bytes)" -f $c.Target, $c.User, $captured[-1].blob_bytes)
    }

    $outFile = Join-Path $outDir 'credentials.json'
    $payload = [ordered]@{
        captured_at = (Get-Date).ToUniversalTime().ToString('o')
        host        = $env:COMPUTERNAME
        user        = $env:USERNAME
        creds       = $captured
        note        = 'Plaintext credentials inside the outer-AES-encrypted .bak. Re-protected via CredWrite (DPAPI) on restore.'
    }
    ($payload | ConvertTo-Json -Depth 6) | Out-File -Encoding UTF8 -LiteralPath $outFile

    return [PSCustomObject]@{
        Layer      = 'wcm'
        Items      = $captured | ForEach-Object { [PSCustomObject]@{ target = $_.target; user = $_.user; bytes = $_.blob_bytes } }
        Count      = $captured.Count
        TotalBytes = (Get-Item -LiteralPath $outFile).Length
    }
}


function Invoke-RestoreWcm {
    <#
    .SYNOPSIS
        Read wcm/credentials.json from the extracted stage dir and CredWrite
        each entry. Idempotent: CredWrite overwrites existing targets.
        DPAPI re-wraps the blob under the target host's current user.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $StageDir,
        [switch] $DryRun
    )

    $credFile = Join-Path $StageDir 'wcm/credentials.json'
    if (-not (Test-Path -LiteralPath $credFile)) {
        Write-Host "  wcm: no credentials.json in .bak — skipping" -ForegroundColor DarkGray
        return [PSCustomObject]@{ Layer='wcm'; Restored=0; Skipped=0; Failed=0; Items=@() }
    }

    $payload = Get-Content -LiteralPath $credFile -Raw | ConvertFrom-Json
    $creds   = @($payload.creds)

    $restored = @()
    $failed   = @()

    foreach ($c in $creds) {
        if ($DryRun) {
            Write-Host ("  [dry-run] would CredWrite target='{0}' user='{1}' ({2} blob bytes)" -f $c.target, $c.user, $c.blob_bytes)
            continue
        }
        try {
            $blob = if ($c.blob_b64) { [Convert]::FromBase64String($c.blob_b64) } else { [byte[]]::new(0) }
            $ok   = [SentinelWcm]::Write($c.target, $c.user, $blob, [int]$c.persist)
            if ($ok) {
                $restored += $c.target
                Write-Verbose "wcm: restored $($c.target)"
            } else {
                $err = [System.ComponentModel.Win32Exception]::new([System.Runtime.InteropServices.Marshal]::GetLastWin32Error())
                $failed += [PSCustomObject]@{ target = $c.target; reason = $err.Message }
                Write-Warning "wcm: CredWrite failed for $($c.target): $($err.Message)"
            }
        } catch {
            $failed += [PSCustomObject]@{ target = $c.target; reason = $_.Exception.Message }
            Write-Warning "wcm: exception restoring $($c.target): $_"
        }
    }

    return [PSCustomObject]@{
        Layer    = 'wcm'
        Restored = $restored.Count
        Skipped  = if ($DryRun) { $creds.Count } else { 0 }
        Failed   = $failed.Count
        Items    = $restored
        Errors   = $failed
    }
}
