# Read + write the SENTBAK file format.
#
# Plaintext (spec §4.1):
#   bytes 0..7    magic = "SENTBAK\0"
#   bytes 8..15   version, ASCII, NUL-padded to 8 (e.g. "v1\0\0\0\0\0\0")
#   bytes 16..271 256-byte UTF-8 JSON header, NUL-padded
#                  { "pillar":"finance", "captured_at":"…",
#                    "encrypted":false, "host":"…", "gz":true }
#   bytes 272..   body = gzip'd tar
#
# Encrypted (spec §4.2, v0.2):
#   bytes 0..271  header (as above, with "encrypted":true)
#   bytes 272..287 salt (16 bytes, random)
#   bytes 288..299 iv   (12 bytes, random)
#   bytes 300..N-17 ciphertext (AES-256-GCM of the gzipped tar)
#   bytes N-16..N-1 auth tag (16 bytes)
# Key derivation: PBKDF2-HMAC-SHA256 over passphrase + salt, 600k iterations,
# 32-byte (256-bit) key.


function Write-SentBakHeader {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [System.IO.Stream] $Stream,
        [Parameter(Mandatory)] [hashtable]        $Header
    )

    # 8-byte magic
    $Stream.Write($script:SentBakMagic, 0, $script:SentBakMagic.Length)

    # 8-byte version, NUL-padded
    $verBytes = New-Object byte[] 8
    $verSrc   = [System.Text.Encoding]::ASCII.GetBytes($script:SentBakVersion)
    [Array]::Copy($verSrc, 0, $verBytes, 0, $verSrc.Length)
    $Stream.Write($verBytes, 0, 8)

    # 256-byte JSON header, NUL-padded
    $json     = ($Header | ConvertTo-Json -Compress -Depth 6)
    $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    if ($jsonBytes.Length -gt $script:SentBakHeaderSz) {
        throw "Bak header JSON is $($jsonBytes.Length) bytes; max $script:SentBakHeaderSz"
    }
    $headerBytes = New-Object byte[] $script:SentBakHeaderSz
    [Array]::Copy($jsonBytes, 0, $headerBytes, 0, $jsonBytes.Length)
    $Stream.Write($headerBytes, 0, $script:SentBakHeaderSz)
}


function Read-SentBakHeader {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "File not found: $Path"
    }

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        # Magic
        $magic = New-Object byte[] 8
        [void]$stream.Read($magic, 0, 8)
        if (-not (Compare-ByteArray $magic $script:SentBakMagic)) {
            throw "Not a SENTBAK file (magic mismatch): $Path"
        }

        # Version
        $verBytes = New-Object byte[] 8
        [void]$stream.Read($verBytes, 0, 8)
        $version = ([System.Text.Encoding]::ASCII.GetString($verBytes)).TrimEnd([char]0)

        # Header
        $headerBytes = New-Object byte[] $script:SentBakHeaderSz
        [void]$stream.Read($headerBytes, 0, $script:SentBakHeaderSz)
        $headerLen = $headerBytes.Length
        # Strip trailing NULs before parsing
        while ($headerLen -gt 0 -and $headerBytes[$headerLen - 1] -eq 0) { $headerLen-- }
        $headerJson = [System.Text.Encoding]::UTF8.GetString($headerBytes, 0, $headerLen)
        $header     = $headerJson | ConvertFrom-Json

        return [PSCustomObject]@{
            Path        = (Resolve-Path -LiteralPath $Path).Path
            Version     = $version
            Header      = $header
            BodyOffset  = 8 + 8 + $script:SentBakHeaderSz   # = 272
            FileSize    = (Get-Item -LiteralPath $Path).Length
        }
    } finally {
        $stream.Dispose()
    }
}


function Compare-ByteArray {
    param([byte[]] $A, [byte[]] $B)
    if ($A.Length -ne $B.Length) { return $false }
    for ($i = 0; $i -lt $A.Length; $i++) {
        if ($A[$i] -ne $B[$i]) { return $false }
    }
    return $true
}


function Test-SecureStringEqual {
    <#
    .SYNOPSIS
        Compare two SecureStrings without exposing their plaintext to PS variables.
    #>
    param(
        [Parameter(Mandatory)] [securestring] $A,
        [Parameter(Mandatory)] [securestring] $B
    )
    $ba = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($A)
    $bb = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($B)
    try {
        $pa = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ba)
        $pb = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bb)
        return ($pa -ceq $pb)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ba)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bb)
    }
}


function Extract-SentBakBody {
    <#
    .SYNOPSIS
        Stream the body (tar.gz) of a .bak into a destination directory.
    .DESCRIPTION
        Slices past the 272-byte header. If the header says encrypted, decrypts
        AES-256-GCM into a temp .tar.gz; otherwise spools the bytes directly.
        Then untars into -DestDir using the bundled tar.exe.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]       $InFile,
        [Parameter(Mandatory)] [string]       $DestDir,
        [securestring]                        $Passphrase
    )

    $info = Read-SentBakHeader -Path $InFile
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

    $tmpTarGz = Join-Path $DestDir ('.body-' + [Guid]::NewGuid().ToString('N') + '.tar.gz')

    if ($info.Header.encrypted) {
        if (-not $Passphrase) {
            $Passphrase = Read-Host -AsSecureString "Passphrase for encrypted .bak"
        }
        try {
            $plain = Read-SentBakEncryptedBody -InFile $InFile -BodyOffset $info.BodyOffset -Passphrase $Passphrase
        } catch {
            throw "Decryption failed (wrong passphrase or corrupted file): $($_.Exception.Message)"
        }
        [System.IO.File]::WriteAllBytes($tmpTarGz, $plain)
        # Zero the plaintext bytes once they're on disk; the temp file will be deleted shortly.
        for ($i = 0; $i -lt $plain.Length; $i++) { $plain[$i] = 0 }
    } else {
        $src = [System.IO.File]::OpenRead($InFile)
        try {
            $src.Position = $info.BodyOffset
            $dst = [System.IO.File]::Create($tmpTarGz)
            try { $src.CopyTo($dst) } finally { $dst.Dispose() }
        } finally { $src.Dispose() }
    }

    & tar.exe -xzf $tmpTarGz -C $DestDir
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed extracting body (exit $LASTEXITCODE)"
    }
    Remove-Item -LiteralPath $tmpTarGz -Force -ErrorAction SilentlyContinue

    return $info
}


# ── encryption primitives (v0.2) ───────────────────────────────────


function New-SentBakKey {
    <#
    .SYNOPSIS
        Derive a 32-byte AES-256 key from a passphrase + salt via PBKDF2-HMAC-SHA256.
    .DESCRIPTION
        600k iterations per spec §4.2. Returns a byte[32]; caller is responsible
        for zeroing it after use.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [securestring] $Passphrase,
        [Parameter(Mandatory)] [byte[]]       $Salt
    )
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Passphrase)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        $pbk = [System.Security.Cryptography.Rfc2898DeriveBytes]::new(
            $plain, $Salt, 600000, [System.Security.Cryptography.HashAlgorithmName]::SHA256
        )
        try { return $pbk.GetBytes(32) } finally { $pbk.Dispose() }
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}


function Write-SentBakEncryptedBody {
    <#
    .SYNOPSIS
        Encrypt $PlainBodyPath (the gzipped tar) and append to $OutStream as
        [salt|iv|ciphertext|tag], per spec §4.2.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [System.IO.Stream] $OutStream,
        [Parameter(Mandatory)] [string]           $PlainBodyPath,
        [Parameter(Mandatory)] [securestring]     $Passphrase
    )

    $salt = New-Object byte[] 16
    $iv   = New-Object byte[] 12
    $rng  = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($salt)
        $rng.GetBytes($iv)
    } finally { $rng.Dispose() }

    $key = New-SentBakKey -Passphrase $Passphrase -Salt $salt
    try {
        $plainBytes = [System.IO.File]::ReadAllBytes($PlainBodyPath)
        $cipher = New-Object byte[] $plainBytes.Length
        $tag    = New-Object byte[] 16
        $gcm = [System.Security.Cryptography.AesGcm]::new($key, 16)
        try {
            $gcm.Encrypt($iv, $plainBytes, $cipher, $tag)
        } finally { $gcm.Dispose() }

        $OutStream.Write($salt,   0, 16)
        $OutStream.Write($iv,     0, 12)
        $OutStream.Write($cipher, 0, $cipher.Length)
        $OutStream.Write($tag,    0, 16)

        # Zero the plaintext we read into memory.
        for ($i = 0; $i -lt $plainBytes.Length; $i++) { $plainBytes[$i] = 0 }
    } finally {
        for ($i = 0; $i -lt $key.Length; $i++) { $key[$i] = 0 }
    }
}


function Read-SentBakEncryptedBody {
    <#
    .SYNOPSIS
        Decrypt the body of an encrypted .bak. Returns the plaintext (gzipped
        tar) as a byte[]. Throws on tag mismatch (wrong passphrase or tampering).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]       $InFile,
        [Parameter(Mandatory)] [int]          $BodyOffset,
        [Parameter(Mandatory)] [securestring] $Passphrase
    )
    $fileSize  = (Get-Item -LiteralPath $InFile).Length
    $cipherLen = [int]($fileSize - $BodyOffset - 16 - 12 - 16)
    if ($cipherLen -lt 0) {
        throw "Encrypted .bak too small to contain salt+iv+tag (file size $fileSize, body offset $BodyOffset)"
    }

    $stream = [System.IO.File]::OpenRead($InFile)
    try {
        $stream.Position = $BodyOffset
        $salt   = New-Object byte[] 16
        [void]$stream.Read($salt, 0, 16)
        $iv     = New-Object byte[] 12
        [void]$stream.Read($iv, 0, 12)
        $cipher = New-Object byte[] $cipherLen
        if ($cipherLen -gt 0) { [void]$stream.Read($cipher, 0, $cipherLen) }
        $tag    = New-Object byte[] 16
        [void]$stream.Read($tag, 0, 16)

        $key = New-SentBakKey -Passphrase $Passphrase -Salt $salt
        try {
            $plain = New-Object byte[] $cipherLen
            $gcm = [System.Security.Cryptography.AesGcm]::new($key, 16)
            try {
                $gcm.Decrypt($iv, $cipher, $tag, $plain)
            } finally { $gcm.Dispose() }
            return $plain
        } finally {
            for ($i = 0; $i -lt $key.Length; $i++) { $key[$i] = 0 }
        }
    } finally {
        $stream.Dispose()
    }
}
