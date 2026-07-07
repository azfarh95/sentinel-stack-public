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
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8098"
_SERVICE = "sentinel-miniapp"


def _keyring_ok() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except Exception:
        return False


def _find_keyring_python() -> list[str] | None:
    """Return the first interpreter cmd that can `import keyring`, or None.

    The token lives in WCM and is only reachable via keyring. When notify.py is
    launched by an interpreter that lacks keyring (e.g. a venv first on PATH),
    we re-exec under a capable one instead of silently sending no token. Tries
    the `py` launcher (canonical Windows → full system install) then a couple of
    well-known install paths. Pure interpreter discovery — touches no secrets.
    """
    candidates: list[list[str]] = []
    pyl = shutil.which("py")
    if pyl:
        candidates.append([pyl, "-3"])
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        for ver in ("Python313", "Python312", "Python311"):
            exe = os.path.join(la, "Programs", "Python", ver, "python.exe")
            if os.path.isfile(exe):
                candidates.append([exe])
    for cmd in candidates:
        try:
            r = subprocess.run([*cmd, "-c", "import keyring"],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                return cmd
        except Exception:
            continue
    return None


def _maybe_reexec_with_keyring(args_token: str | None) -> None:
    """If this interpreter can't read WCM, relaunch once under one that can."""
    if os.environ.get("_NOTIFY_REEXEC"):          # already re-exec'd — don't loop
        return
    if args_token or os.environ.get("NOTIFY_TOKEN"):  # token available without keyring
        return
    if _keyring_ok():
        return
    cmd = _find_keyring_python()
    if not cmd:
        return  # nothing better found; fall through and let the bridge decide
    env = {**os.environ, "_NOTIFY_REEXEC": "1"}
    proc = subprocess.run([*cmd, os.path.abspath(__file__), *sys.argv[1:]], env=env)
    raise SystemExit(proc.returncode)


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

    # PATH-robustness: if the launching interpreter can't read the WCM token,
    # relaunch once under one that can (no-op when keyring/env token present).
    _maybe_reexec_with_keyring(args.token)

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
