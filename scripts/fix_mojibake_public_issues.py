"""One-shot fixer: re-fetch each private issue body fresh, write UTF-8
to a temp file, and update the corresponding public issue via gh edit
--body-file. Bypasses the cp1252-mangling that happened when the original
sync passed bodies via --body argument.

Reads the mapping at workspace/reminders/issue-sync-mapping.json.
Reapplies the same sanitization rules used in sync_issues_to_public.py
so the public bodies stay consistent with the original sanitization
intent.

Idempotent — safe to re-run.

Usage:
    python fix_mojibake_public_issues.py            # fix all
    python fix_mojibake_public_issues.py --dry-run  # preview only
    python fix_mojibake_public_issues.py --only 25  # one specific public#
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Re-use the sanitization rules + footer from the sync script
# Note: sync_issues_to_public's module-level code already wraps sys.stdout
# in a UTF-8 TextIOWrapper, so we don't need to do it ourselves here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_issues_to_public import (  # noqa: E402
    sanitize, SYNC_FOOTER, PRIVATE_REPO, PUBLIC_REPO, MAPPING_FILE, gh,
)


def fetch_private_body(issue_num: int) -> tuple[str, str]:
    """Returns (title, body) of private issue, fresh from API in UTF-8."""
    out = gh("issue", "view", str(issue_num), "--repo", PRIVATE_REPO,
             "--json", "title,body")
    j = json.loads(out)
    return j.get("title", ""), j.get("body", "")


def update_public_body(public_num: int, title: str, body: str, dry_run: bool):
    """Update via --body-file (UTF-8) — bypasses the cp1252-on-argv bug."""
    if dry_run:
        print(f"  [dry] would update public#{public_num}: {len(body)} chars body, title='{title[:60]}'")
        return
    # Write to a UTF-8 file (no BOM)
    fd, tmp_path = tempfile.mkstemp(suffix=".md", text=False)
    os.close(fd)
    try:
        Path(tmp_path).write_text(body, encoding="utf-8")
        # Update body-file + title (in case title also got mangled)
        gh("issue", "edit", str(public_num), "--repo", PUBLIC_REPO,
           "--title", title, "--body-file", tmp_path)
    finally:
        try: os.unlink(tmp_path)
        except Exception: pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", type=int, help="Specific public issue # to fix")
    args = ap.parse_args()

    if not MAPPING_FILE.exists():
        sys.exit(f"Mapping not found: {MAPPING_FILE}")
    mapping = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(mapping)} mappings\n")

    fixed, errors = 0, []
    for private_str, public_num in sorted(mapping.items(), key=lambda x: int(x[0])):
        private_num = int(private_str)
        if args.only and public_num != args.only:
            continue
        try:
            title, raw_body = fetch_private_body(private_num)
            sanitized = sanitize(raw_body) + SYNC_FOOTER
            sanitized_title = sanitize(title)
            update_public_body(public_num, sanitized_title, sanitized, args.dry_run)
            fixed += 1
            print(f"  ✅ private#{private_num} → public#{public_num}  ({len(sanitized)} chars)")
        except Exception as e:
            errors.append((private_num, public_num, str(e)[:200]))
            print(f"  ❌ private#{private_num} → public#{public_num}  ERROR: {e}")

    print(f"\n=== Summary ===")
    print(f"  fixed:  {fixed}")
    print(f"  errors: {len(errors)}")
    if errors:
        for pn, pubn, err in errors:
            print(f"    private#{pn} → public#{pubn}: {err}")


if __name__ == "__main__":
    main()
