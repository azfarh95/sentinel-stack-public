# set_sentinel_menu_button.ps1
# Pins a permanent Web App launcher to the YourSentinelBot's menu button (the
# button left of the message input). Tapping it opens the Sentinel finance
# dashboard inside Telegram's WebView.
#
# Re-run after changing the URL or label. Idempotent (replaces the prior
# button).

$ErrorActionPreference = "Stop"

$envFile = "C:\Users\azfar\metamcp-local\.env.local"
$token = (Get-Content $envFile | Select-String "^TELEGRAM_BOT_TOKEN=" | ForEach-Object { ($_ -split "=", 2)[1].Trim() } | Select-Object -First 1)
if (-not $token) { throw "TELEGRAM_BOT_TOKEN not found in $envFile" }

$webAppUrl = "https://your-domain.example.com/"
$buttonLabel = "Open Sentinel"

# Telegram's setChatMenuButton with no chat_id sets the default menu button
# for all private chats with this bot — including future first-touch users.
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
    Write-Host "✓ Menu button set on YourSentinelBot:"
    Write-Host "    label: $buttonLabel"
    Write-Host "    url:   $webAppUrl"
    Write-Host ""
    Write-Host "Open Telegram → AZ-Sentinel-AI chat → tap the menu button"
    Write-Host "(left of the message input field) to launch the dashboard."
} else {
    Write-Host "FAILED: $resp"
    exit 1
}
