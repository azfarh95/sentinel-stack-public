# register_bot_commands.ps1
# Pushes our slash command catalog to @YourSentinelBot via Telegram's
# setMyCommands API so they show up in the bot's UI command menu.
#
# Re-run after adding new commands. Idempotent (replaces the full list).

$ErrorActionPreference = "Stop"

$envFile = "C:\Users\azfar\metamcp-local\.env.local"
$token = (Get-Content $envFile | Select-String "^TELEGRAM_BOT_TOKEN=" | ForEach-Object { ($_ -split "=", 2)[1].Trim() } | Select-Object -First 1)
if (-not $token) { throw "TELEGRAM_BOT_TOKEN not found in $envFile" }

# Telegram constraint: command names must be lowercase letters/digits/underscores only.
# Descriptions: 3-256 chars.
$commands = @(
    @{command="wallet_snapshot"; description="Show portfolio across all chains + staking"},
    @{command="cashflow";        description="Next month's debt obligation + projections"},
    @{command="balance";         description="Net worth (assets - liabilities)"},
    @{command="balance_sheet";   description="IAS 1 balance sheet (current vs non-current)"},
    @{command="dashboard";       description="Open the Sentinel mini app"},
    @{command="save_new";        description="Save session memory then reset"},
    @{command="memory_update";   description="Flush session memory now"},
    @{command="new";             description="Reset conversation context"}
)

$payload = @{ commands = $commands } | ConvertTo-Json -Depth 4

$resp = curl.exe -s -X POST "https://api.telegram.org/bot$token/setMyCommands" `
    -H "Content-Type: application/json" -d $payload
$ok = ($resp | ConvertFrom-Json).ok
if ($ok) {
    Write-Host "Registered $($commands.Count) commands on @YourSentinelBot:"
    foreach ($c in $commands) {
        Write-Host ("  /{0,-18} {1}" -f $c.command, $c.description)
    }
} else {
    Write-Host "FAILED: $resp"
    exit 1
}
