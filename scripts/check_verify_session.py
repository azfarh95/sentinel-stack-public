"""What happened to the verification prompt session?"""
import io, json, sys
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

recent = [e for e in events if e.get("ts","") >= "2026-05-09T18:37:00"]
print(f"Events after verification prompt: {len(recent)}")

c = Counter(e.get("type","?") for e in recent)
for t, count in sorted(c.items()):
    print(f"  {t}: {count}")

print()
ends = [e for e in recent if e.get("type") == "session.ended"]
for e in ends:
    ts = e["ts"][:19]
    status = e["data"].get("status", "?")
    print(f"  ended {ts} status={status}")

completes = [e for e in recent if e.get("type") == "model.completed"]
print(f"\n  model.completed in period: {len(completes)}")
for cm in completes:
    u = cm["data"].get("usage", {})
    print(f"    {cm['ts'][:19]} in={u.get('input',0)} out={u.get('output',0)} aborted={cm['data'].get('aborted')} timedOut={cm['data'].get('timedOut')}")

# Look at session.started for the verify prompt
prompts = [e for e in recent if e.get("type") == "prompt.submitted"]
print(f"\n  prompt.submitted: {len(prompts)}")
for p in prompts:
    pr = p["data"].get("prompt", "")[:80]
    print(f"    {p['ts'][:19]} {pr!r}")
