"""Compare turn 1 vs turn 2 of session 7 to extract turn 2's NEW tool calls only."""
import io
import json
import sys
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TJ = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions\e806f43b-f2e2-4b83-8642-4f80a89c3f36.trajectory.jsonl")

with open(TJ, encoding="utf-8") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

turns = [e for e in events if e.get("type") == "model.completed"]
print(f"Total turns in trajectory: {len(turns)}")
# Last 2 turns are the failed run + retry
if len(turns) >= 2:
    t1 = turns[-2]
    t2 = turns[-1]

    def extract_tool_calls(turn):
        calls = []
        for msg in turn["data"].get("messagesSnapshot", []):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", []) or []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    calls.append(c.get("name", "?"))
        return calls

    def extract_assistant_texts(turn):
        texts = []
        for msg in turn["data"].get("messagesSnapshot", []):
            if msg.get("role") != "assistant":
                continue
            for c in msg.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    texts.append(c.get("text", ""))
        return texts

    t1_calls = extract_tool_calls(t1)
    t2_calls = extract_tool_calls(t2)

    print(f"\nTurn 1 ({t1['ts'][:19]}): {len(t1_calls)} cumulative tool calls")
    print(f"Turn 2 ({t2['ts'][:19]}): {len(t2_calls)} cumulative tool calls")
    print(f"NEW in turn 2: {len(t2_calls) - len(t1_calls)}")

    # Diff
    t1_counter = Counter(t1_calls)
    t2_counter = Counter(t2_calls)
    new_calls = (t2_counter - t1_counter)
    print(f"\nNEW tool calls made by turn 2:")
    for name, cnt in new_calls.most_common():
        print(f"  {name}: {cnt}")
    if not new_calls:
        print("  (none — turn 2 made no new tool calls)")

    # Latest assistant texts (the actual reply user saw)
    print(f"\nTurn 2 assistant texts:")
    t2_texts = extract_assistant_texts(t2)
    new_texts = t2_texts[len(extract_assistant_texts(t1)):]
    for i, t in enumerate(new_texts):
        print(f"\n  [text {i+1}] ({len(t)} chars):")
        print("  " + t[:600].replace("\n", "\n  "))

    # Tool result failures in turn 2 messagesSnapshot
    print(f"\nTool results since turn 1:")
    t2_msgs = t2["data"].get("messagesSnapshot", [])
    t1_msg_count = len(t1["data"].get("messagesSnapshot", []))
    new_msgs = t2_msgs[t1_msg_count:]
    fails = []
    oks = []
    for msg in new_msgs:
        if msg.get("role") == "toolResult":
            for c in (msg.get("content", []) or []):
                if isinstance(c, dict) and c.get("type") == "text":
                    txt = str(c.get("text", ""))
                    if "Session not found" in txt or "Transport not found" in txt or "failed" in txt.lower()[:50]:
                        fails.append(txt[:150])
                    else:
                        oks.append(txt[:150])
    print(f"  fails: {len(fails)}")
    for f in fails[:3]: print(f"    {f}")
    print(f"  oks:   {len(oks)}")
    for o in oks[:5]: print(f"    {o[:100]}")
