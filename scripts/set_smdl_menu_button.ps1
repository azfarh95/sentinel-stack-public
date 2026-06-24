# set_smdl_menu_button.ps1
# Pins a permanent Web App launcher to the SM-DL bot's menu button (left of
# the message input field). Tapping it opens the SMDL Mini App in Telegram's
# WebView. Idempotent — re-run after URL or label changes.

$ErrorActionPreference = "Stop"

$envFile = "C:\Users\azfar\metamcp-local\.env.local"
$token = (Get-Content $envFile | Select-String "^SMDL_BOT_TOKEN=" | ForEach-Object { ($_ -split "=", 2)[1].Trim() } | Select-Object -First 1)
if (-not $token) { throw "SMDL_BOT_TOKEN not found in $envFile" }

$webAppUrl = "https://media.your-domain.example.com/app"
$buttonLabel = "Open SMDL"

$payload = @{
    menu_button = @{
        type = "web_app"
        text = $buttonLabel
        web_app = @{ url = $webAppUrl }
    }
} | ConvertTo-Json -Depth 5 -Compress

$resp = curl.exe -s -X POST "https://api.telegram.org/bot$token/setChatMenuButton" `
    -H "Content-Type: application/json" -d $payload
$parsed = $resp | ConvertFrom-Json
if ($parsed.ok) {
    Write-Host "✓ Menu button set on SM-DL bot:"
    Write-Host "    label: $buttonLabel"
    Write-Host "    url:   $webAppUrl"
    Write-Host ""
    Write-Host "Open Telegram → SM-DL chat → close and reopen if needed."
} else {
    Write-Host "FAILED: $resp"
    exit 1
}
