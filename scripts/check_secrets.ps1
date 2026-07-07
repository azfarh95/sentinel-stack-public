# check_secrets.ps1
# Scans staged files for plaintext secret patterns. Exit 1 if any found.
# Wire into git pre-commit hook:
#   echo 'powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_secrets.ps1' > .git/hooks/pre-commit
#
# Or run manually:
#   pwsh scripts/check_secrets.ps1

$patterns = @(
    'sk-or-v1-[a-zA-Z0-9]{32,}',           # OpenRouter
    'sk-lm-[a-zA-Z0-9:]{16,}',             # LM Studio
    'sk-proj-[a-zA-Z0-9]{32,}',            # OpenAI project keys
    'sk-ant-[a-zA-Z0-9_-]{80,}',           # Anthropic
    'ghp_[a-zA-Z0-9]{36,}',                # GitHub PAT classic
    'gho_[a-zA-Z0-9]{36,}',                # GitHub OAuth
    'ghs_[a-zA-Z0-9]{36,}',                # GitHub server-to-server
    'AIza[a-zA-Z0-9_-]{35}',               # Google API
    'AKIA[A-Z0-9]{16}',                    # AWS access key
    'tvly-(?:dev-)?[a-zA-Z0-9]{20,}',      # Tavily
    'hf_[a-zA-Z0-9]{30,}',                 # HuggingFace
    'xoxb-[a-zA-Z0-9-]{50,}',              # Slack bot token
    'xoxp-[a-zA-Z0-9-]{50,}',              # Slack user token
    '[0-9]{8,}:AA[a-zA-Z0-9_-]{30,}'       # Telegram bot token
)

# Get list of files to scan: staged in git, or all tracked if not in a hook context.
try {
    $files = & git diff --cached --name-only --diff-filter=ACM 2>$null
    if (-not $files) { $files = & git ls-files 2>$null }
} catch {
    Write-Error "git not available or not a repo"
    exit 0
}

# Skip the template file (it has placeholders that look like secrets to humans
# but aren't), the gitignored env files (these never reach git anyway),
# and any backup files from the sync script.
$skip = @(
    '\.env\.local$',
    '\.env\.local\.backup-',
    '\.env\.local\.template$',
    'scripts/check_secrets\.ps1$',         # this file mentions patterns
    'scripts/sync_env_from_wcm\.ps1$',     # references key names
    '\.svg$',                              # SVG path data has false-positive AKIA matches
    'send_ig\.py$',                        # gitignored, untracked
    '\.git/'
)

$violations = @()
foreach ($f in $files) {
    if (-not $f) { continue }
    if (-not (Test-Path $f)) { continue }
    $skipFile = $false
    foreach ($s in $skip) { if ($f -match $s) { $skipFile = $true; break } }
    if ($skipFile) { continue }
    $content = Get-Content $f -Raw -ErrorAction SilentlyContinue
    if (-not $content) { continue }
    foreach ($p in $patterns) {
        if ($content -match $p) {
            $violations += "  $f → matched pattern: $p"
        }
    }
}

if ($violations.Count -gt 0) {
    Write-Host ""
    Write-Host "Plaintext secret pattern(s) detected:" -ForegroundColor Red
    $violations | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    Write-Host ""
    Write-Host "Move secrets to Windows Credential Manager and use placeholders." -ForegroundColor Yellow
    Write-Host "See scripts/sync_env_from_wcm.ps1 for the pattern." -ForegroundColor Yellow
    exit 1
}

Write-Host "[check_secrets] No plaintext secret patterns detected." -ForegroundColor Green
exit 0
