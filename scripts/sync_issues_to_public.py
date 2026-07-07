"""Sync GitHub issues from YOUR_GITHUB_USERNAME/sentinel-stack (private) → YOUR_GITHUB_USERNAME/sentinel-stack-public.

Sanitizes any owner-specific or secret-shaped content using the same patterns
as scripts/sanitize-public.sh, plus extra defensive scrubs (token-shaped strings,
internal IDs, etc.).

Idempotent: maintains a mapping at workspace/reminders/issue-sync-mapping.json
so re-runs skip already-synced issues. Use --force to re-sync (will create
duplicates — careful).

Usage:
    python sync_issues_to_public.py --dry-run       # preview without posting
    python sync_issues_to_public.py                 # post to public repo
    python sync_issues_to_public.py --skip 15,16    # skip specific issue numbers
    python sync_issues_to_public.py --only 24,25,26 # only sync these
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

PRIVATE_REPO = "YOUR_GITHUB_USERNAME/sentinel-stack"
PUBLIC_REPO  = "YOUR_GITHUB_USERNAME/sentinel-stack-public"
MAPPING_FILE = Path(__file__).resolve().parent.parent / "workspace" / "reminders" / "issue-sync-mapping.json"

SANITIZE_RULES = [
    # Owner identity
    (r"azfardajiwang@gmail\.com", "your@email.com"),
    (r"\bazfar\b(?!h95/sentinel-stack-public)", "YOUR_USER"),  # username in paths
    (r"\bAzfar Hakim\b",          "Your Name"),

    # Telegram identifiers
    (r"\bYOUR_TELEGRAM_CHAT_ID\b",            "YOUR_TELEGRAM_CHAT_ID"),
    (r"\b-1003748374568\b",       "YOUR_TELEGRAM_GROUP_ID"),
    (r"\bYourSentinelBot\b",      "YourSentinelBot"),
    (r"\bYourWatchdogBot\b", "YourWatchdogBot"),
    (r"\bYourSMDLBot\b",       "YourSMDLBot"),
    (r"\bSentinelClaudeAssistantBot\b", "YourTestBot"),

    # Domains
    (r"\bsentinel\.az-sentinel\.xyz\b", "your-domain.example.com"),
    (r"\baz-sentinel\.xyz\b",          "your-domain.example.com"),

    # Azure resource names
    (r"sentinel-openclaw-docintel\.cognitiveservices\.azure\.com",
     "your-resource.cognitiveservices.azure.com"),

    # Specific calendar ID (real one)
    (r"3554ab9f457bc4501f369d3158b18d175c2c388682d448250487c788131a058b@group\.calendar\.google\.com",
     "YOUR_CALENDAR_ID@group.calendar.google.com"),

    # Real-world client/agency names worth keeping out of public
    (r"\bYourAgency\b", "YourAgency"),
    (r"\byouragency\b", "youragency"),

    # Repo URL fix-up (after the username sed runs)
    (r"YOUR_USER/sentinel-stack(?!-public)", f"{PUBLIC_REPO}"),
    (r"YOUR_USER/metamcp-local",            f"{PUBLIC_REPO.split('/')[0]}/metamcp-local"),

    # Defensive: token-shaped strings (Telegram bot token format)
    (r"\b\d{9,12}:[A-Za-z0-9_-]{30,}\b", "[REDACTED_BOT_TOKEN]"),

    # Defensive: long hex tokens (>=32 char hex strings)
    (r"\b[a-f0-9]{32,}\b", "[REDACTED_HEX_TOKEN]"),

    # Defensive: sk_/sk- prefixed keys
    (r"\bsk[_-][A-Za-z0-9_-]{20,}\b", "[REDACTED_API_KEY]"),

    # Defensive: tvly- prefixed (Tavily)
    (r"\btvly[_-][A-Za-z0-9_-]{10,}\b", "[REDACTED_TAVILY_KEY]"),

    # Bearer tokens in Authorization headers
    (r"(?i)Bearer\s+[A-Za-z0-9_.\-]{20,}", "Bearer [REDACTED]"),

    # File paths
    (r"C:\\Users\\azfar\\", r"C:\\Users\\YOUR_USER\\"),
    (r"/home/azfar/",       "/home/YOUR_USER/"),
    (r"\\\\wsl\.localhost\\Ubuntu-24\.04\\home\\azfar\\",
     r"\\\\wsl.localhost\\Ubuntu-24.04\\home\\YOUR_USER\\"),
]

SYNC_FOOTER = (
    "\n\n---\n*Synced from internal tracker on 2026-05-10. Owner-specific identifiers "
    "(emails, Telegram IDs, bot usernames, domain names, file paths) replaced with "
    "placeholders. Token-shaped strings auto-redacted defensively.*"
)

# Issues that should never be synced
SKIP_BY_DEFAULT = {15}  # "test issue"


def sanitize(text: str) -> str:
    """Apply all sanitization rules to a string."""
    if not text:
        return text
    for pattern, replacement in SANITIZE_RULES:
        text = re.sub(pattern, replacement, text)
    return text


def gh(*args: str) -> str:
    """Run gh CLI, return stdout. Forces UTF-8 decoding because Python's
    subprocess on Windows defaults to cp1252, which mangles emoji + arrows
    + em-dashes that gh outputs as UTF-8."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True,
                       encoding="utf-8", check=False)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def list_private_issues() -> list[dict]:
    """Get all issues (open + closed) on the private repo."""
    out = gh("issue", "list", "--repo", PRIVATE_REPO, "--state", "all", "--limit", "100",
             "--json", "number,title,body,state,labels,createdAt")
    return json.loads(out)


def load_mapping() -> dict:
    if not MAPPING_FILE.exists():
        return {}
    return json.loads(MAPPING_FILE.read_text(encoding="utf-8"))


def save_mapping(m: dict):
    MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_FILE.write_text(json.dumps(m, indent=2), encoding="utf-8")


def create_public_issue(title: str, body: str, labels: list[str], dry_run: bool = False) -> int | None:
    """Create on public repo. Returns new issue number, or None in dry-run."""
    if dry_run:
        return None
    args = ["issue", "create", "--repo", PUBLIC_REPO, "--title", title, "--body", body]
    for lbl in labels:
        args += ["--label", lbl]
    out = gh(*args).strip()
    # gh returns the URL — extract the issue number from the trailing /N
    m = re.search(r"/issues/(\d+)\s*$", out)
    return int(m.group(1)) if m else None


def close_public_issue(issue_num: int, comment: str | None = None, dry_run: bool = False):
    if dry_run:
        return
    args = ["issue", "close", str(issue_num), "--repo", PUBLIC_REPO]
    if comment:
        args += ["--comment", comment]
    gh(*args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip",    default="", help="Comma-separated issue numbers to skip")
    ap.add_argument("--only",    default="", help="Comma-separated issue numbers to include (overrides default include)")
    ap.add_argument("--force",   action="store_true", help="Re-sync issues already in mapping (creates dupes)")
    args = ap.parse_args()

    skip_set = set(SKIP_BY_DEFAULT)
    if args.skip:
        skip_set.update(int(x) for x in args.skip.split(",") if x.strip())
    only_set = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None

    mapping = load_mapping()
    issues = list_private_issues()
    print(f"Found {len(issues)} issues on {PRIVATE_REPO}\n")

    summary = {"synced": [], "skipped": [], "already_synced": [], "errors": []}

    for issue in sorted(issues, key=lambda i: i["number"]):
        n = issue["number"]
        title = issue["title"]
        state = issue["state"]
        body = issue.get("body", "") or ""

        if only_set is not None and n not in only_set:
            continue
        if n in skip_set:
            summary["skipped"].append({"n": n, "title": title, "reason": "in skip list"})
            print(f"  ⏭  #{n} skipped (in skip list)")
            continue
        if str(n) in mapping and not args.force:
            summary["already_synced"].append({"private": n, "public": mapping[str(n)]})
            print(f"  ✓ #{n} already synced → public#{mapping[str(n)]}")
            continue

        sanitized_body = sanitize(body) + SYNC_FOOTER
        sanitized_title = sanitize(title)
        labels = [l["name"] for l in issue.get("labels", [])]

        if args.dry_run:
            print(f"  [dry-run] would create on {PUBLIC_REPO}: '{sanitized_title}'  [labels: {labels}]")
            print(f"            body length: {len(body)} → {len(sanitized_body)} chars (after sanitize)")
            redacted = len(re.findall(r"\[REDACTED_[A-Z_]+\]", sanitized_body))
            placeholders = len(re.findall(r"YOUR_(USER|TELEGRAM_CHAT_ID|TELEGRAM_GROUP_ID|CALENDAR_ID)", sanitized_body))
            print(f"            redactions: {redacted} token-shaped, {placeholders} placeholders")
            continue

        try:
            new_n = create_public_issue(sanitized_title, sanitized_body, [], False)
            if new_n is None:
                summary["errors"].append({"n": n, "error": "create returned no number"})
                continue
            mapping[str(n)] = new_n
            save_mapping(mapping)
            if state == "CLOSED":
                close_public_issue(new_n, comment="Closed in source tracker.", dry_run=False)
            summary["synced"].append({"private": n, "public": new_n, "state": state})
            print(f"  ✅ #{n} → public#{new_n}  ({state})")
        except Exception as e:
            summary["errors"].append({"n": n, "error": str(e)[:200]})
            print(f"  ❌ #{n} ERROR: {e}")

    print(f"\n=== Summary ===")
    print(f"  synced:         {len(summary['synced'])}")
    print(f"  already-synced: {len(summary['already_synced'])}")
    print(f"  skipped:        {len(summary['skipped'])}")
    print(f"  errors:         {len(summary['errors'])}")
    if summary["errors"]:
        print("\nErrors:")
        for e in summary["errors"]:
            print(f"  #{e['n']}: {e['error']}")


if __name__ == "__main__":
    main()
