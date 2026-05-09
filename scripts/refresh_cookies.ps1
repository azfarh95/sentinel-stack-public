# refresh_cookies.ps1 — Pull browser cookies for yt-dlp / gallery-dl
# Writes Netscape-format cookies.txt files to G:\YT-DLP\cookies\
# which is bind-mounted into the ytdlp-mcp container at /cookies

$CookieDir = "G:\YT-DLP\cookies"

# --- ProgId → yt-dlp browser name (known browsers) ---
$progIdMap = @{
    "BraveHTML"   = "brave"
    "ChromeHTML"  = "chrome"
    "MSEdgeHTM"   = "edge"
    "FirefoxURL"  = "firefox"
    "VivaldiHTML" = "vivaldi"
    "OperaStable" = "opera"
    "ChromiumHTM" = "chromium"
}

# --- Chromium-based browsers not in yt-dlp's list ---
# Use "chrome:PATH" — yt-dlp applies Chrome DPAPI decryption on any Chromium profile
$chromiumCompat = [ordered]@{
    "comet"      = "$env:LOCALAPPDATA\Comet\User Data\Default"
    "perplexity" = "$env:LOCALAPPDATA\Perplexity\Comet\User Data\Default"
    "arc"        = "$env:LOCALAPPDATA\Arc\User Data\Default"
    "thorium"    = "$env:LOCALAPPDATA\Thorium\User Data\Default"
}

# --- Folder-existence fallback for known yt-dlp browsers ---
$knownFolders = [ordered]@{
    "brave"    = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data"
    "chrome"   = "$env:LOCALAPPDATA\Google\Chrome\User Data"
    "edge"     = "$env:LOCALAPPDATA\Microsoft\Edge\User Data"
    "vivaldi"  = "$env:LOCALAPPDATA\Vivaldi\User Data"
    "chromium" = "$env:LOCALAPPDATA\Chromium\User Data"
    "firefox"  = "$env:APPDATA\Mozilla\Firefox\Profiles"
}

# --- Resolve browser ---
$BrowserArg = $null

# 1. Try Windows default browser via registry
$progId = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice" -ErrorAction SilentlyContinue).ProgId
if ($progId) {
    $matched = $progIdMap.Keys | Where-Object { $progId -like "$_*" } | Select-Object -First 1
    if ($matched) {
        $BrowserArg = $progIdMap[$matched]
    }
}

# 2. Unknown Chromium-based default → use chrome:PATH
if (-not $BrowserArg) {
    foreach ($name in $chromiumCompat.Keys) {
        if (Test-Path $chromiumCompat[$name]) {
            $BrowserArg = "chrome:$($chromiumCompat[$name])"
            Write-Host "Browser: $name (Chromium-compat → $BrowserArg)"
            break
        }
    }
}

# 3. Fallback: first installed known browser by folder
if (-not $BrowserArg) {
    $found = $knownFolders.Keys | Where-Object { Test-Path $knownFolders[$_] } | Select-Object -First 1
    if ($found) { $BrowserArg = $found }
}

if (-not $BrowserArg) {
    Write-Error "No supported browser detected. Exiting."
    exit 1
}

if ($BrowserArg -notmatch ":") {
    Write-Host "Browser: $BrowserArg (default)"
}

# --- Sites to refresh ---
$Sites = [ordered]@{
    "instagram" = "https://www.instagram.com/"
    "tiktok"    = "https://www.tiktok.com/"
    "youtube"   = "https://www.youtube.com/"
}

# --- Run ---
New-Item -ItemType Directory -Force -Path $CookieDir | Out-Null

$ok = 0; $fail = 0

foreach ($name in $Sites.Keys) {
    $out = Join-Path $CookieDir "$name.txt"
    $url = $Sites[$name]
    Write-Host "  Refreshing $name..." -NoNewline
    yt-dlp --cookies-from-browser $BrowserArg `
           --cookies $out `
           --skip-download $url `
           --quiet 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host " done"
        $ok++
    } else {
        Write-Host " FAILED (are you logged into $name in your browser?)"
        $fail++
    }
}

Write-Host ""
Write-Host "Cookies refreshed: $ok ok, $fail failed"
Write-Host "Output dir: $CookieDir"
