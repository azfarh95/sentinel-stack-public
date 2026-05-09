#!/usr/bin/env bash
set -euo pipefail

# ── Remove private files ──────────────────────────────────────────────────────
rm -rf workspace/memory/
rm -rf workspace/proposals/
rm -f workspace/RETRY_TEST_1.md workspace/SENTINEL_TEST_PLAN.md
rm -f workspace/TEST_EXECUTION_RESULTS.md workspace/LIVE_TEST_SCRIPT.md
rm -f workspace/FLAGSHIP_MODEL_RECOMMENDATION_PROMPT.md
rm -f workspace/PROMPT_TELEGRAM_FORMAT.txt
rm -f workspace/STARTUP_VERIFICATION.md 2>/dev/null || true
rm -f docker-compose.firefly.yml
rm -f send_ig.py
rm -f sentinel_config.json
rm -f invalidation.md recent-updates.md 2>/dev/null || true
rm -f STARTUP_VERIFICATION.md 2>/dev/null || true

# ── Replace SOUL.md with generic template ────────────────────────────────────
cat > workspace/SOUL.md << 'SOULEOF'
# SOUL.md — Agent System Prompt

This file is loaded by OpenClaw as the agent's system prompt. Customise it for your own setup.

## Tool Discipline

**Use dedicated MCP tools — never fall back to web search when a purpose-built tool exists.**

- **Maps / directions / navigation → `maps_directions` or `maps_search`** — never use web search for these.
- **Video downloads → `VideoDownloader__download_video`** — never tell the user to download manually.
- **Reminders / scheduling → Reminders MCP tools** — never ask the user to set it themselves.
- **Calendar queries ("do I have X tomorrow?") → `calendar_list_events`** — never claim you lack calendar access.

## Calendar Integration

Configure your own calendar integration here. Example:

**Calendar ID:** `YOUR_CALENDAR_ID@group.calendar.google.com`

When the user asks about calendar events, call `calendar_list_events` with the appropriate
`calendar_id` and `time_min` (ISO 8601 with timezone offset).

When the user sends an event update (e.g. "8 May — Team Meeting"), find the existing event
and update it via `calendar_update_event`.

## Commands

| Command | Response |
|---|---|
| `/dashboard` | Reply with your Mini App URL — nothing else. |
| `/memory-update` or `/save-new` | Flush session memories, reply with summary. |
| `/new` | Cleared by OpenClaw automatically. |

## Memory

Use `memory-mcp__memory_store`, `memory-mcp__memory_search`, `memory-mcp__memory_list`
for persistent cross-session recall.

## Vibe

Be the assistant you'd actually want to talk to. Concise when needed, thorough when it matters.
SOULEOF

# ── Replace TOOLS.md with generic template ───────────────────────────────────
cat > workspace/TOOLS.md << 'TOOLSEOF'
# TOOLS.md — Environment-Specific Tool Notes

Add your own environment-specific tool notes here. Examples:

### SSH hosts
- my-server → 192.168.1.100, user: admin

### Camera names
- living-room → Main area, wide angle

### TTS
- Preferred voice: Nova
TOOLSEOF

# Remove TOOL_DECISION_GUIDE if it exists (personal routing rules)
rm -f workspace/TOOL_DECISION_GUIDE.md 2>/dev/null || true

# ── Sanitize personal identifiers in all text files ──────────────────────────
find . -type f \( \
  -name "*.md" -o -name "*.py" -o -name "*.yml" -o -name "*.yaml" \
  -o -name "*.json" -o -name "*.sh" -o -name "*.bat" -o -name "*.ps1" \
  -o -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.txt" \
  -o -name "*.sql" -o -name "*.cfg" -o -name "*.ini" \
\) \
  -not -path "./.git/*" \
  -not -path "./node_modules/*" \
  -not -path "./apps/*/node_modules/*" \
  -not -path "./.github/workflows/*" \
| while read -r f; do
    sed -i \
      -e 's/YOUR_GITHUB_USERNAME/YOUR_GITHUB_USERNAME/g' \
      -e 's/azfardajiwang@gmail\.com/your@email\.com/g' \
      -e 's/YOUR_TELEGRAM_CHAT_ID/YOUR_TELEGRAM_CHAT_ID/g' \
      -e 's/YourSentinelBot/YourSentinelBot/g' \
      -e 's/YourNanobotBot/YourNanobotBot/g' \
      -e 's/YourWatchdogBot/YourWatchdogBot/g' \
      -e 's/sentinel\.az-sentinel\.xyz/your-domain\.example\.com/g' \
      -e 's/az-sentinel\.xyz/your-domain\.example\.com/g' \
      -e 's/sentinel-openclaw-docintel\.cognitiveservices\.azure\.com/your-resource\.cognitiveservices\.azure\.com/g' \
      -e 's/3554ab9f457bc4501f369d3158b18d175c2c388682d448250487c788131a058b@group\.calendar\.google\.com/YOUR_CALENDAR_ID@group\.calendar\.google\.com/g' \
      -e 's/YourAgency/YourAgency/g' \
      -e 's/youragency/youragency/g' \
      "$f" 2>/dev/null || true
done

# ── Restore correct public-repo URLs ──────────────────────────────────────────
# Blanket YOUR_GITHUB_USERNAME -> YOUR_GITHUB_USERNAME above breaks every legitimate link
# pointing to the public repo. Repair them: point clone URLs, releases, and
# issues at the actual public repo (YOUR_GITHUB_USERNAME/sentinel-stack-public).
find . -type f \( -name "*.md" -o -name "*.json" -o -name "*.yml" -o -name "*.yaml" \) \
  -not -path "./.git/*" \
  -not -path "./node_modules/*" \
  -not -path "./apps/*/node_modules/*" \
| while read -r f; do
    sed -i \
      -e 's|YOUR_GITHUB_USERNAME/sentinel-stack|YOUR_GITHUB_USERNAME/sentinel-stack-public|g' \
      "$f" 2>/dev/null || true
done

# ── Inject GitHub badges at top of README ────────────────────────────────────
if [ -f README.md ]; then
  badges='[![GitHub stars](https://img.shields.io/github/stars/YOUR_GITHUB_USERNAME/sentinel-stack-public?style=flat&color=yellow)](https://github.com/YOUR_GITHUB_USERNAME/sentinel-stack-public/stargazers)
[![License](https://img.shields.io/github/license/YOUR_GITHUB_USERNAME/sentinel-stack-public?style=flat&color=blue)](https://github.com/YOUR_GITHUB_USERNAME/sentinel-stack-public/blob/master/LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/YOUR_GITHUB_USERNAME/sentinel-stack-public?style=flat&color=green)](https://github.com/YOUR_GITHUB_USERNAME/sentinel-stack-public/commits/master)
[![Topic: self-hosted](https://img.shields.io/badge/topic-self--hosted-orange)](https://github.com/topics/self-hosted)
[![Topic: mcp](https://img.shields.io/badge/topic-mcp-purple)](https://github.com/topics/mcp)
[![Topic: telegram-bot](https://img.shields.io/badge/topic-telegram--bot-blue)](https://github.com/topics/telegram-bot)

'
  # Insert badges after the first line (which is the # H1 title)
  awk -v badges="$badges" 'NR==1 {print; print ""; print badges; next} {print}' README.md > README.md.tmp
  mv README.md.tmp README.md
fi

echo "Sanitization complete."
