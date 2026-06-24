#!/usr/bin/env python
"""
notify.py — fire a Sentinel notification from any Claude session / shell.

Posts to the bridge's /api/notify (localhost:8098 by default), which stores it
for the Suite app's in-app feed and/or pushes it via @Sentinel_claude_testbot_bot.

Token: read from WCM (sentinel-miniapp/notify_token), env NOTIFY_TOKEN, or --token.
If no token is configured anywhere, the bridge still accepts localhost callers.

Examples:
    py notify.py "Build finished" "all green on main"
    py notify.py --level success --channel both "Deploy done"
    py notify.py --channel bot --title "Heads up" --body "tests flaky"
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8098"
_SERVICE = "sentinel-miniapp"


def _resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("NOTIFY_TOKEN")
    if env:
        return env
    try:
        import keyring
        return keyring.get_password(_SERVICE, "notify_token") or ""
    except Exception:
        return ""


def main() -> int:
    p = argparse.ArgumentParser(description="Send a Sentinel notification.")
    p.add_argument("title", nargs="?", default="", help="Notification title")
    p.add_argument("body", nargs="?", default="", help="Notification body")
    p.add_argument("--title", dest="title_opt", default=None)
    p.add_argument("--body", dest="body_opt", default=None)
    p.add_argument("--level", default="info",
                   choices=["info", "success", "warning", "error"])
    p.add_argument("--channel", default="both", choices=["app", "bot", "both"])
    p.add_argument("--event", default="general",
                   help="Event category (e.g. 'idle'); gated by owner prefs server-side.")
    p.add_argument("--source", default="claude")
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--token", default=None)
    p.add_argument("--document", default=None,
                   help="Path to a file to send to Telegram as a document (≤50MB). "
                        "Routes to /api/notify-document; --body becomes the caption.")
    args = p.parse_args()

    title = args.title_opt if args.title_opt is not None else args.title
    body = args.body_opt if args.body_opt is not None else args.body

    # Document mode: send a FILE to the owner on Telegram (sibling of the text path).
    if args.document:
        caption = (f"{title}\n{body}".strip() if title or body else "")
        payload = {"path": os.path.abspath(args.document), "caption": caption}
        endpoint = "/api/notify-document"
    else:
        if not title and not body:
            p.error("provide a title and/or body (positional or --title/--body)")
        payload = {"title": title, "body": body, "level": args.level,
                   "channel": args.channel, "event": args.event, "source": args.source}
        endpoint = "/api/notify"

    req = urllib.request.Request(
        args.base.rstrip("/") + endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    token = _resolve_token(args.token)
    if token:
        req.add_header("X-Notify-Token", token)

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"notify failed: HTTP {e.code} {e.read().decode(errors='replace')}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"notify failed: {e}\n")
        return 1

    print(json.dumps(res))
    bot = (res.get("result") or {}).get("bot")
    if bot and not bot.get("ok"):
        sys.stderr.write(f"warning: bot push failed: {bot.get('error')}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
