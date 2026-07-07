"""Did Sentinel actually call the tools this time after the verification challenge?"""
import io, json, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")
VERIFY_PROMPT_KEY = "Quick verification check"

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

turns = [e for e in events if e.get("type") == "model.completed"]
last = turns[-1]

print(f"Last turn: {last['ts']}")
u = last["data"].get("usage", {})
print(f"  tokens: in={u.get('input',0):,} out={u.get('output',0):,}")

snap = last["data"].get("messagesSnapshot", [])

# Find verification prompt
verify_idx = None
for i, msg in enumerate(snap):
    if msg.get("role") != "user": continue
    content = msg.get("content", "")
    text = ""
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict): text += c.get("text", "")
    else:
        text = str(content)
    if "verification check" in text.lower() or "claimed in your last reply" in text.lower():
        verify_idx = i
        break

# If not found, list all user messages so we can see what's there
if verify_idx is None:
    print("DIAG: All user messages in this snapshot:")
    for i, msg in enumerate(snap):
        if msg.get("role") != "user": continue
        c = msg.get("content", "")
        if isinstance(c, list):
            text = "".join(cc.get("text","") for cc in c if isinstance(cc, dict))
        else:
            text = str(c)
        print(f"  snap[{i}] user: {text[:120].replace(chr(10), ' ')}")
    sys.exit(0)

print(f"Verification prompt at snap[{verify_idx}]; total len={len(snap)}")
print()

# All tool calls after verification prompt
calls = []
for msg in snap[verify_idx + 1:]:
    if msg.get("role") != "assistant": continue
    for c in (msg.get("content", []) or []):
        if isinstance(c, dict) and c.get("type") == "toolCall":
            calls.append({"name": c.get("name", "?"), "args": c.get("input", c.get("arguments", {}))})

print(f"Tool calls in verification turn: {len(calls)}")
for tc in calls:
    args_str = json.dumps(tc["args"])[:300]
    print(f"  {tc['name']}")
    print(f"    args: {args_str}")
print()

# Tool results — did the calls succeed?
print("=== Tool results ===")
for msg in snap[verify_idx + 1:]:
    if msg.get("role") != "toolResult": continue
    for c in (msg.get("content", []) or []):
        if isinstance(c, dict) and c.get("type") == "text":
            txt = c.get("text", "")[:300]
            print(f"  RESULT: {txt}")
            print()

# Final agent reply
print("=== Final assistant text ===")
texts = last["data"].get("assistantTexts", [])
# Note: assistantTexts is per-turn, not per-message — the final element is the LATEST reply
# Find the texts that appeared AFTER the verification prompt
# Heuristic: the last few elements of assistantTexts
for i, t in enumerate(texts[-3:]):
    print(f"\n--- text {len(texts) - 3 + i + 1} ({len(t)} chars) ---")
    print(t)
