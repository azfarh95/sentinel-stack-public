"""Extract calendar events created in session 7-8 — full payload."""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

# Get the LAST model.completed turn (turn 8 = retry)
turns = [e for e in events if e.get("type") == "model.completed"]
last = turns[-1]

# Walk messagesSnapshot — pair toolCall with adjacent toolResult
snap = last["data"].get("messagesSnapshot", [])

calendar_events = []
for i, msg in enumerate(snap):
    if msg.get("role") != "assistant":
        continue
    for c in (msg.get("content", []) or []):
        if isinstance(c, dict) and c.get("type") == "toolCall":
            name = c.get("name", "")
            if "calendar_create_event" not in name:
                continue
            args = c.get("input", c.get("arguments", {}))
            # Find the next toolResult
            result_text = ""
            for j in range(i + 1, min(i + 4, len(snap))):
                if snap[j].get("role") == "toolResult":
                    for rc in (snap[j].get("content", []) or []):
                        if isinstance(rc, dict) and rc.get("type") == "text":
                            result_text = rc.get("text", "")
                            break
                    break
            calendar_events.append({"args": args, "result": result_text})

print(f"calendar_create_event invocations: {len(calendar_events)}")
print()
for i, ev in enumerate(calendar_events, 1):
    args = ev["args"]
    print(f"=== Event {i} (the args agent sent) ===")
    print(f"  summary:     {args.get('summary')}")
    print(f"  start:       {args.get('start')}")
    print(f"  end:         {args.get('end')}")
    print(f"  recurrence:  {args.get('recurrence')}")
    print(f"  reminders:   {args.get('reminders')}")
    print(f"  calendar_id: {args.get('calendar_id', '')[:40]}...")
    desc = args.get('description', '')
    if desc:
        print(f"  description: {desc[:300]}")
    result = ev["result"]
    if result:
        try:
            r = json.loads(result)
            print(f"\n  RESULT (parsed):")
            print(f"    id:      {r.get('id')}")
            print(f"    summary: {r.get('summary')}")
            print(f"    start:   {r.get('start')}")
            print(f"    end:     {r.get('end')}")
            print(f"    htmlLink: {r.get('htmlLink', '')[:80]}")
        except json.JSONDecodeError:
            print(f"\n  RESULT (raw, first 400 chars):")
            print(f"    {result[:400]}")
    print()
