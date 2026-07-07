#!/usr/bin/env python
"""Drive the browser assistant with YOUR OWN task (the fenced harness + local Qwen).

Examples
--------
  python run.py "go to news.ycombinator.com and list the top 3 story titles"
  python run.py --steps 20 --wall 360 "fill the contact form on example.com with name=Az, msg=hi, submit"
  python run.py --comet "summarise the page that's open in my browser"
  python run.py --vision "..."     # screenshots ON (slower; only helps DOM-blind pages)

Modes
-----
  (default)  throwaway HEADLESS Chrome — isolated, never touches your real browser.
  --comet    ATTACH to your running Comet/Chrome via CDP (default :9222). Launch it
             first with comet-sidepanel/Launch-Comet-CDP.ps1 (--remote-debugging-port=9222).
             The agent drives YOUR live tabs — it will NOT close the browser on exit.

Prereqs: local Qwen up on the infer-bridge :8095. The whole run is fenced
(max steps + wall-clock) so a stuck task self-terminates and frees the GPU slot.
NOTE: no approval-gate yet — in --comet mode it can click/type on real pages.
Keep tasks read-only there until Phase 4 lands the approval gate.
"""
import argparse
import asyncio
import json
import sys

from agent_runner import bridge_active, run_task


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a browser task via local Qwen (fenced).")
    ap.add_argument("task", help="the task to perform, in plain English")
    ap.add_argument("--comet", action="store_true",
                    help="attach to the running Comet via CDP (else throwaway headless)")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222", help="CDP url for --comet")
    ap.add_argument("--steps", type=int, default=12, help="max agent steps (default 12)")
    ap.add_argument("--wall", type=float, default=240.0, help="wall-clock fence seconds (default 240)")
    ap.add_argument("--vision", action="store_true",
                    help="enable screenshots (slower; only for DOM-blind pages)")
    ap.add_argument("--gated", action="store_true",
                    help="approval-gate state-changing actions (click/type/...) via console y/N")
    ap.add_argument("--no-gate", action="store_true",
                    help="force the gate OFF even with --comet (use with care on real pages)")
    ap.add_argument("--telegram", action="store_true",
                    help="approval prompts go to Telegram (tap ✓/✗ from your phone) — for headless runs")
    ap.add_argument("--approve-timeout", type=float, default=180.0,
                    help="seconds to wait for a Telegram approval before denying (default 180)")
    args = ap.parse_args()

    from mode import browser_enabled
    if not browser_enabled():
        print("[run] browser mode is DISABLED (kill-switch). Re-enable: python mode.py on", flush=True)
        return 3

    # Gate ON by default when attached to the real Comet (real logged-in sessions);
    # opt-in otherwise. --no-gate forces it off. Channel: Telegram (headless-friendly)
    # or the console y/N default.
    gated = (args.gated or args.comet or args.telegram) and not args.no_gate
    approve = None
    if gated and args.telegram:
        from approval_telegram import load_testbot_creds, make_telegram_approver
        token, chat = load_testbot_creds()
        if not token:
            print("[run] --telegram needs TESTBOT_TOKEN in metamcp-local/.env.local", flush=True)
            return 2
        approve = make_telegram_approver(token, chat, timeout_s=args.approve_timeout)
    elif gated:
        from tools_gated import console_approve
        approve = console_approve

    try:
        print(f"[run] :8095 active slots before: {bridge_active()}", flush=True)
    except Exception:
        print("[run] warning: couldn't reach the infer-bridge :8095 — is local Qwen up?", flush=True)

    print(f"[run] mode={'comet/CDP ' + args.cdp if args.comet else 'throwaway-headless'} "
          f"steps={args.steps} wall={args.wall}s vision={args.vision} "
          f"gate={('ON via ' + ('Telegram' if args.telegram else 'console')) if gated else 'off'}", flush=True)

    rec = asyncio.run(run_task(
        args.task, label="cli", max_steps=args.steps, max_wall_s=args.wall,
        use_vision=args.vision, cdp_url=(args.cdp if args.comet else None),
        approve=approve, persist=True))

    print(json.dumps(rec, indent=2, ensure_ascii=False))
    if rec.get("final"):
        print("\n=== RESULT ===\n" + str(rec["final"]))
    return 0 if rec.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
