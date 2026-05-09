# notify_owner.ps1
# Sends a Telegram message via @SentinelClaudeAssistantBot to the owner's DM.
# Used by Claude during autopilot work to ping for smoke tests, surface blockers,
# or report milestones. Token is the testbot's bot token (read from .env.testenv).
#
# Usage:
#   .\scripts\notify_owner.ps1 -Subject "Phase 1.0 ready" -Message "Open mini app, tap Browser, send a query."
#   .\scripts\notify_owner.ps1 -Message "Blocker: CDP exposure check failed" -Urgent

param(
    [Parameter(Mandatory=$true)]
    [string]$Message,
    [string]$Subject = "",
    [switch]$Urgent
)

$envFile = "C:\Users\azfar\.claude\projects\Projects-Proposal-WIP\V4\ClaudeAssistant\.env.testenv"
if (-not (Test-Path $envFile)) { Write-Error "env not found: $envFile"; exit 1 }

$token = (Get-Content $envFile | Select-String "^TESTBOT_TOKEN=" | ForEach-Object { ($_ -split "=", 2)[1].Trim() } | Select-Object -First 1)
$chatId = YOUR_TELEGRAM_CHAT_ID

if (-not $token) { Write-Error "No TESTBOT_TOKEN in $envFile"; exit 1 }

$prefix = if ($Urgent) { "🚨 BLOCKER" } else { "🤖 Claude" }
$body = if ($Subject) { "$prefix - $Subject`n`n$Message" } else { "$prefix`n`n$Message" }

$response = curl.exe -s -F "chat_id=$chatId" -F "text=$body" -F "parse_mode=HTML" "https://api.telegram.org/bot$token/sendMessage"
$ok = ($response | ConvertFrom-Json).ok
if ($ok) { Write-Host "[notify] sent" } else { Write-Host "[notify] FAIL: $response" }
