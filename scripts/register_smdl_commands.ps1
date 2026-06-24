# register_smdl_commands.ps1
# Pushes the SM-DL bot's slash command catalog to Telegram via setMyCommands
# so they show up in the bot's UI command menu. Token = SMDL_BOT_TOKEN.
#
# Re-run after adding new commands. Idempotent (replaces the full list).

$ErrorActionPreference = "Stop"

$envFile = "C:\Users\azfar\metamcp-local\.env.local"
$token = (Get-Content $envFile | Select-String "^SMDL_BOT_TOKEN=" | ForEach-Object { ($_ -split "=", 2)[1].Trim() } | Select-Object -First 1)
if (-not $token) { throw "SMDL_BOT_TOKEN not found in $envFile" }

# Telegram constraint: command names lowercase letters/digits/underscores only.
$commands = @(
    @{command="start";              description="Welcome + open dashboard / get access code"},
    @{command="regenerate_token";   description="Get a fresh access code (1-min expiry)"},
    @{command="dashboard";          description="Open the SM-DL mini app"},
    @{command="watch";       description="Add a streamer/channel URL to the watchlist"},
    @{command="unwatch";     description="Remove a URL from the watchlist"},
    @{command="watchlist";   description="Show current watchlist"},
    @{command="live_status"; description="Show active live recordings"},
    @{command="storage_stats"; description="Disk usage in the downloads folder"},
    @{command="clear_cache"; description="Forget the URL cache (downloads stay)"},
    @{command="language";    description="Change language (en, ru)"},
    @{command="timezone";    description="Set your timezone offset"},
    @{command="scrape_add";    description="Add an IG/TikTok profile to auto-monitor (owner)"},
    @{command="scrape_remove"; description="Remove a profile from auto-monitor (owner)"},
    @{command="scrape_list";   description="List auto-monitored profiles + status (owner)"},
    @{command="scrape_pause";  description="Pause polling for one profile (owner)"},
    @{command="scrape_resume"; description="Resume polling, reset failure count (owner)"},
    @{command="scrape_now";    description="Force one immediate probe (owner)"}
)

$payload = @{ commands = $commands } | ConvertTo-Json -Depth 4

$resp = curl.exe -s -X POST "https://api.telegram.org/bot$token/setMyCommands" `
    -H "Content-Type: application/json" -d $payload
$ok = ($resp | ConvertFrom-Json).ok
if ($ok) {
    Write-Host "Registered $($commands.Count) commands on the SM-DL bot:"
    foreach ($c in $commands) {
        Write-Host ("  /{0,-14} {1}" -f $c.command, $c.description)
    }
} else {
    Write-Host "FAILED: $resp"
    exit 1
}
