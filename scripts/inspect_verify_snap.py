"""Look at the verification turn's messagesSnapshot structure."""
import io, json, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

turns = [e for e in events if e.get("type") == "model.completed"]
print(f"Total turns: {len(turns)}")
print(f"Last turn: {turns[-1]['ts']}")

last = turns[-1]
snap = last["data"].get("messagesSnapshot", [])
print(f"messagesSnapshot length: {len(snap)}")
print()

# Print roles + first 80 chars of each msg
print("Snapshot summary (role + first 80 chars):")
for i, msg in enumerate(snap):
    role = msg.get("role", "?")
    content = msg.get("content", "")
    text = ""
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    text += c.get("text", "")
                elif c.get("type") == "thinking":
                    text += "[thinking] "
                elif c.get("type") == "toolCall":
                    text += f"[toolCall:{c.get('name','?')}] "
                elif c.get("type") == "toolResult":
                    text += "[toolResult] "
    else:
        text = str(content)
    text = text.replace("\n", " ")[:120]
    print(f"  snap[{i}] {role}: {text}")

# Also look at assistantTexts
texts = last["data"].get("assistantTexts", [])
print(f"\nassistantTexts count: {len(texts)}")
for i, t in enumerate(texts[-5:]):
    idx = len(texts) - 5 + i + 1
    print(f"\n--- assistantText {idx} ({len(t)} chars) ---")
    print(t[:1500])
