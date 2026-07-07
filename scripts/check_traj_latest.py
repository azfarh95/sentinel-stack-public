"""Show last 10 trajectory events to debug timestamp filtering."""
import io, json, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")
with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

print(f"Total events: {len(events)}")
print(f"\nLast 10 events:")
for e in events[-10:]:
    ts = e.get("ts", "")
    typ = e.get("type", "?")
    print(f"  {ts[:19]}  {typ}")
