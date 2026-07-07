"""Did Sentinel ACTUALLY call calendar_update_event in the correction turn?
Or did it just say so without actually doing it (announce-then-skip)?"""
import io, json, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

turns = [e for e in events if e.get("type") == "model.completed"]
last = turns[-1]

# Get cumulative messages from last turn
snap = last["data"].get("messagesSnapshot", [])

# Find correction msg index
correction_idx = None
for i, msg in enumerate(snap):
    if msg.get("role") != "user":
        continue
    content = msg.get("content", "")
    text = ""
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                text += c.get("text", "")
            elif isinstance(c, str):
                text += c
    else:
        text = str(content)
    if "Correction on the statement dates" in text:
        correction_idx = i
        break

print(f"Correction msg at snap[{correction_idx}]; total snap len={len(snap)}")
print()

# Walk EVERYTHING after correction_idx and list every tool call name + first 200 chars of args
print("=== Every tool call after correction ===")
tool_calls_after = []
for i, msg in enumerate(snap[correction_idx + 1:], correction_idx + 1):
    if msg.get("role") != "assistant":
        continue
    content = msg.get("content", []) or []
    if not isinstance(content, list):
        continue
    for c in content:
        if isinstance(c, dict) and c.get("type") == "toolCall":
            name = c.get("name", "?")
            args = c.get("input", c.get("arguments", {}))
            args_str = json.dumps(args)[:300]
            tool_calls_after.append((i, name, args_str))

for idx, name, args in tool_calls_after:
    print(f"  snap[{idx}] {name}")
    print(f"    args: {args}")
    print()

print(f"Total tool calls after correction: {len(tool_calls_after)}")
print()

# Check for ANY tool call mentioning calendar
calendar_calls = [t for t in tool_calls_after if "calendar" in t[1].lower()]
print(f"Calendar tool calls: {len(calendar_calls)}")

# Scan tool RESULTS for confirmation of any update
print()
print("=== Tool RESULTS that look like calendar updates or successes ===")
for i, msg in enumerate(snap[correction_idx + 1:], correction_idx + 1):
    if msg.get("role") != "toolResult":
        continue
    content = msg.get("content", []) or []
    if not isinstance(content, list):
        continue
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            txt = c.get("text", "")
            if any(k in txt.lower() for k in ["updated", "calendar", "event", "patch"])[:200]:
                print(f"  snap[{i}] result: {txt[:300]}")
