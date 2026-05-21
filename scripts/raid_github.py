"""GitHub raid — read raid-scope.yaml, fetch upstream activity per entry,
synthesize a Telegram-ready report.

Run: python scripts/raid_github.py [--telegram]
  --telegram  send the report to chat YOUR_TELEGRAM_CHAT_ID via the watchdog bot
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCOPE_FILE = REPO_ROOT / "workspace" / "raid-scope.yaml"
WINDOW_DAYS = 14

# ── Lightweight YAML loader (yaml lib not always installed; parse what we need) ──
import yaml  # PyYAML — bundled with most Python distros via pip

# Critical labels to flag in open issues
CRITICAL_LABELS = {"security", "breaking", "regression", "vulnerability", "critical"}


def gh(args: list[str], json_out: bool = True) -> object:
    """Invoke gh CLI; return parsed JSON or raw stdout."""
    cmd = ["gh"] + args
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    if json_out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None
    return r.stdout


def fetch_release(repo: str) -> dict | None:
    data = gh([
        "release", "view", "--repo", repo,
        "--json", "tagName,name,publishedAt,body,isPrerelease,url",
    ])
    if not data:
        return None
    body = (data.get("body") or "")[:600]
    return {
        "tag":    data.get("tagName"),
        "name":   data.get("name") or data.get("tagName"),
        "date":   data.get("publishedAt"),
        "url":    data.get("url"),
        "body":   body,
        "is_pre": bool(data.get("isPrerelease")),
    }


def fetch_recent_commits(repo: str, days: int = WINDOW_DAYS) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = gh([
        "api", f"/repos/{repo}/commits?since={since}&per_page=30",
    ])
    if not isinstance(data, list):
        return []
    out = []
    for c in data[:30]:
        msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0][:120]
        author = ((c.get("commit") or {}).get("author") or {}).get("name", "?")
        sha = (c.get("sha") or "")[:7]
        out.append({"sha": sha, "msg": msg, "author": author})
    return out


def fetch_critical_issues(repo: str) -> list[dict]:
    """Open issues with security/breaking/regression labels."""
    data = gh([
        "api", f"/repos/{repo}/issues?state=open&per_page=50",
    ])
    if not isinstance(data, list):
        return []
    out = []
    for issue in data:
        if issue.get("pull_request"):
            continue  # skip PRs
        labels = {(l.get("name") or "").lower() for l in (issue.get("labels") or [])}
        if not (labels & CRITICAL_LABELS):
            continue
        out.append({
            "num":   issue.get("number"),
            "title": (issue.get("title") or "")[:120],
            "labels": sorted(labels & CRITICAL_LABELS),
            "url":   issue.get("html_url"),
        })
    return out[:5]


def score_repo(release: dict | None, commits: list, issues: list) -> str:
    """Return health emoji: 🟢 / 🟡 / 🔴."""
    if issues:
        return "🔴"
    if release:
        body = (release.get("body") or "").lower()
        if any(k in body for k in ("breaking", "deprecat", "security", "vulnerability", "cve-")):
            return "🟡"
    if len(commits) >= 20:
        return "🟡"  # high activity = read details
    return "🟢"


def fmt_date_short(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]


def fmt_repo(entry: dict, release, commits, issues) -> tuple[str, str]:
    """Return (badge, formatted-block)."""
    badge = score_repo(release, commits, issues)
    name = entry["name"]
    repo = entry["repo"]
    bullets = []

    if "release" in entry["watch"]:
        if release:
            tag = release["tag"] or "?"
            d = fmt_date_short(release["date"])
            pre = " [pre]" if release["is_pre"] else ""
            bullets.append(f"  • {tag}{pre} ({d})")
        else:
            bullets.append("  • no releases")

    if "commits" in entry["watch"]:
        n = len(commits)
        if n == 0:
            bullets.append(f"  • 0 commits in {WINDOW_DAYS}d")
        else:
            bullets.append(f"  • {n} commits in {WINDOW_DAYS}d")
            # First-line of top 2 commit messages
            for c in commits[:2]:
                bullets.append(f"      {c['sha']} {c['msg'][:70]}")

    if "issues_open" in entry["watch"]:
        if issues:
            bullets.append(f"  • {len(issues)} critical-labelled open issue(s):")
            for i in issues[:3]:
                lbls = ",".join(i["labels"])
                bullets.append(f"      #{i['num']} [{lbls}] {i['title'][:60]}")
        else:
            bullets.append(f"  • no critical-labelled open issues")

    # No bullet truncation: critical-issue lines and recent-commit context are
    # exactly the "why this matters" — losing them defeats the purpose. Length
    # cap is at the Telegram-send chunking layer instead.
    block = f"{badge} {name} ({repo})\n" + "\n".join(bullets)
    return badge, block


def main() -> None:
    # Force UTF-8 stdout/stderr — Windows console defaults to cp1252 and
    # chokes on emoji output.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Send report to chat YOUR_TELEGRAM_CHAT_ID")
    parser.add_argument("--print",    action="store_true", help="Print report to stdout (default)")
    args = parser.parse_args()

    with open(SCOPE_FILE) as f:
        scope = yaml.safe_load(f)
    entries = scope["default_scope"]

    by_category: dict[str, list[tuple[str, str]]] = {}
    actions: list[str] = []

    print(f"raid: {len(entries)} repos to check", file=sys.stderr)
    for i, e in enumerate(entries, 1):
        print(f"  [{i}/{len(entries)}] {e['repo']}", file=sys.stderr)
        watch = e.get("watch", [])
        release = fetch_release(e["repo"]) if "release" in watch else None
        commits = fetch_recent_commits(e["repo"]) if "commits" in watch else []
        issues  = fetch_critical_issues(e["repo"]) if "issues_open" in watch else []

        badge, block = fmt_repo(e, release, commits, issues)
        cat = e.get("category", "other")
        by_category.setdefault(cat, []).append((badge, block))

        if badge == "🔴":
            actions.append(f"  • {e['name']}: review {len(issues)} critical issue(s) → https://github.com/{e['repo']}/issues")
        elif badge == "🟡":
            actions.append(f"  • {e['name']}: read release notes → {release['url'] if release else 'commits'}")

    # Synthesize report
    now = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    parts = [f"🛡 GitHub Raid — {now}", f"{WINDOW_DAYS}-day window across {len(entries)} repos", ""]
    for cat in sorted(by_category):
        title = cat.replace("_", " ").title()
        parts.append(f"\n── {title} ──")
        for _, block in sorted(by_category[cat], key=lambda x: x[0]):
            parts.append(block)

    if actions:
        parts.append("\nRecommended actions")
        parts.extend(actions)
    else:
        parts.append("\nNo action recommended — all green.")

    parts.append("\n_via GitHub_")
    report = "\n".join(parts)

    if args.telegram:
        import urllib.parse, urllib.request
        # Get watchdog token from WCM
        import keyring
        tok = keyring.get_password("sentinel-watchdog", "bot_token")
        if not tok:
            print("watchdog bot token not in WCM", file=sys.stderr)
            sys.exit(1)
        # Telegram has 4096-char limit per message — split if needed
        chunks = []
        cur = ""
        for line in report.split("\n"):
            if len(cur) + len(line) + 1 > 3900:
                chunks.append(cur)
                cur = line
            else:
                cur = (cur + "\n" + line) if cur else line
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            data = urllib.parse.urlencode({
                "chat_id": "YOUR_TELEGRAM_CHAT_ID",
                "text":    chunk,
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                data=data,
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_body = resp.read().decode()
                if '"ok":true' not in resp_body:
                    print(f"telegram error: {resp_body}", file=sys.stderr)
        print(f"raid: sent {len(chunks)} message(s) to Telegram", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
