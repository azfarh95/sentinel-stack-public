"""Track B — P1c (iframe only): the one P1c case that didn't run (the suite crashed
on a cp1252 print before reaching it). TinyMCE editor INSIDE an iframe — a known-hard
case for DOM agents (the editable body is in a nested browsing context)."""
import asyncio

from agent_runner import run_task
from p1_suite import gate


async def main():
    h = gate()
    gated = h.get("ok") and h.get("backend") == "ready" and not h.get("active")
    print(f"=== p1c-iframe === gate backend={h.get('backend')} active={h.get('active')} run={gated}")
    if not gated:
        print(f"SKIPPED — {h}"); return
    r = await run_task(
        "Go to https://the-internet.herokuapp.com/iframe . There is a rich-text editor "
        "embedded inside an IFRAME. Clear it and type the text 'hello sentinel' into the "
        "editor, then report the editor's current text content.",
        label="p1c-iframe", max_steps=12, max_wall_s=220, use_vision=False)
    print(f"-> {r['status']} | steps={r['steps']} dur={r['dur_s']}s")
    print(f"   final: {str(r['final'])[:300]}")
    if r.get("err"):
        print(f"   err: {r['err']}")


if __name__ == "__main__":
    asyncio.run(main())
