"""Analyze the correction turn — did Sentinel actually read PDFs this time?"""
import io
import json
import sys
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")
CORRECTION_START = "2026-05-09T18:27:00"  # 02:27 SGT

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

# Find the correction turn (latest model.completed after the correction prompt)
turns = [e for e in events if e.get("type") == "model.completed" and e.get("ts","") >= CORRECTION_START]
print(f"model.completed turns since correction: {len(turns)}")

if not turns:
    print("No turns yet")
    sys.exit(0)

last = turns[-1]
u = last["data"].get("usage", {})
print(f"Last turn at {last['ts'][:19]}")
print(f"  tokens in/out: {u.get('input',0):,} / {u.get('output',0):,}")
print()

# Extract NEW tool calls (vs the prior turn 8 baseline of 19)
snap = last["data"].get("messagesSnapshot", [])

# Find the index where THIS user message appears
correction_msg_idx = None
for i, msg in enumerate(snap):
    if msg.get("role") == "user":
        content = msg.get("content", "")
        if isinstance(content, list):
            text = "".join(c.get("text","") for c in content if isinstance(c, dict))
        else:
            text = str(content)
        if "Correction on the statement dates" in text:
            correction_msg_idx = i
            break

if correction_msg_idx is None:
    print("Could not find correction user message in snapshot")
    sys.exit(0)

print(f"Correction message at snap[{correction_msg_idx}]; analyzing assistant turns AFTER that")
print()

# Walk messages after that index, collect tool calls + assistant texts
tool_calls = []
tool_results = []
assistant_texts = []
for msg in snap[correction_msg_idx + 1:]:
    if msg.get("role") == "assistant":
        content = msg.get("content", []) or []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "toolCall":
                        tool_calls.append({
                            "name": c.get("name", "?"),
                            "args": c.get("input", c.get("arguments", {})),
                        })
                    elif c.get("type") == "text":
                        assistant_texts.append(c.get("text", ""))
    elif msg.get("role") == "toolResult":
        for c in (msg.get("content", []) or []):
            if isinstance(c, dict) and c.get("type") == "text":
                tool_results.append(c.get("text", "")[:300])

# Tool call summary
print(f"Tool calls in correction response: {len(tool_calls)}")
counter = Counter(tc["name"] for tc in tool_calls)
for name, cnt in counter.most_common():
    print(f"  {name}: {cnt}")
print()

# Did the agent actually read PDFs? Look for onedrive/file/read tool calls
pdf_read_calls = [tc for tc in tool_calls if any(k in tc["name"].lower() for k in ["onedrive", "read", "fetch", "file"])]
print(f"PDF/file-read tool calls: {len(pdf_read_calls)}")
for tc in pdf_read_calls[:5]:
    args_str = json.dumps(tc["args"])[:200]
    print(f"  {tc['name']}: {args_str}")
print()

# Did the agent UPDATE the calendar event?
calendar_updates = [tc for tc in tool_calls if "calendar_update" in tc["name"].lower() or "calendar_patch" in tc["name"].lower()]
print(f"Calendar UPDATE tool calls: {len(calendar_updates)}")
for tc in calendar_updates:
    args_str = json.dumps(tc["args"])[:300]
    print(f"  {tc['name']}: {args_str}")
print()

# Final assistant texts
print(f"Final assistant texts (last reply to user):")
for txt in assistant_texts[-2:]:
    print()
    print(txt[:1500])
    print("..." if len(txt) > 1500 else "")
