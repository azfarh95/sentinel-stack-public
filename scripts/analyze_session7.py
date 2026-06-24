"""One-off: analyze session 7 (Bills calendar request) of the e806f43b trajectory."""
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")
SESSION_7_START = "2026-05-09T17:39:19"

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

session_7 = [e for e in events if e.get("ts", "") >= SESSION_7_START]
print(f"Total events in trajectory: {len(events)}")
print(f"Session 7 events (after {SESSION_7_START}): {len(session_7)}")
print(f"Type breakdown for session 7:")
for t, c in sorted(Counter(e.get("type", "?") for e in session_7).items()):
    print(f"  {t}: {c}")

print()

ends = [e for e in session_7 if e.get("type") == "session.ended"]
turns = [e for e in session_7 if e.get("type") == "model.completed"]

print(f"Session 7 model.completed events: {len(turns)}")
total_in, total_out = 0, 0
for i, t in enumerate(turns, 1):
    u = t["data"].get("usage", {})
    print(f"  turn {i}: {t['ts'][:19]}  in={u.get('input',0):>7,}  out={u.get('output',0):>5,}  total={u.get('total',0):>7,}")
    total_in += u.get("input", 0)
    total_out += u.get("output", 0)

print()
print(f"Aggregate session 7: in={total_in:,}  out={total_out:,}  total={total_in+total_out:,}")

if ends:
    e = ends[-1]
    end_ts = e["ts"]
    print(f"Session ended: {end_ts[:19]}  status={e['data'].get('status', '?')}")
    d_start = datetime.fromisoformat(SESSION_7_START + "+00:00")
    d_end = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
    elapsed = (d_end - d_start).total_seconds()
    print(f"Wall-clock: {elapsed:.0f}s ({elapsed/60:.1f} min)")
else:
    print("Session 7 still in flight (no session.ended yet)")

# Tool calls across ALL session 7 turns
print()
print("Tool calls in session 7 (cumulative across all turns):")
all_tool_calls = []
for t in turns:
    snap = t["data"].get("messagesSnapshot", [])
    for msg in snap:
        if msg.get("role") == "assistant":
            content = msg.get("content", []) or []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "toolCall":
                        all_tool_calls.append(c.get("name", "?"))
# Note: messagesSnapshot is cumulative, so last turn has all of them
last_snap = turns[-1]["data"].get("messagesSnapshot", []) if turns else []
session_7_tools = []
for msg in last_snap:
    if msg.get("role") == "assistant":
        # Only count assistant messages that came AFTER session 7 start
        # (they don't have timestamps so we use a heuristic: last N turns of messagesSnapshot)
        for c in (msg.get("content", []) or []):
            if isinstance(c, dict) and c.get("type") == "toolCall":
                session_7_tools.append(c.get("name", "?"))

# This counts ALL tool calls in the cumulative snapshot — overcounts. But the breakdown is informative.
print(f"  total tool calls in cumulative snapshot: {len(session_7_tools)}")
for name, cnt in Counter(session_7_tools).most_common():
    print(f"    {name}: {cnt}")
