"""Print Sentinel's full reply to the correction message."""
import io, json, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

turns = [e for e in events if e.get("type") == "model.completed"]
last = turns[-1]
print(f"Last turn: {last['ts']}")

# The data has 'assistantTexts' field directly
texts = last["data"].get("assistantTexts", [])
print(f"assistantTexts entries: {len(texts)}")
print()
for i, t in enumerate(texts, 1):
    print(f"=== assistantText[{i}] ({len(t)} chars) ===")
    print(t)
    print()
