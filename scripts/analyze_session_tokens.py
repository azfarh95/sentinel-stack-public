"""Quick analyzer for OpenClaw session trajectory token usage.

Usage:
    python analyze_session_tokens.py <path-to-trajectory.jsonl>

Or with no arg, picks the most recently modified trajectory.jsonl in
~/.openclaw/agents/main/sessions/ (via WSL UNC path).
"""
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout on Windows so unicode glyphs (delta, etc.) work
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)


def find_latest_trajectory():
    sessions = Path(r"\\wsl.localhost\Ubuntu-24.04\home\azfar\.openclaw\agents\main\sessions")
    if not sessions.is_dir():
        return None
    trajs = list(sessions.glob("*.trajectory.jsonl"))
    if not trajs:
        return None
    return max(trajs, key=lambda p: p.stat().st_mtime)


def analyze(path):
    print(f"Trajectory: {path}")
    print(f"Size: {path.stat().st_size:,} bytes\n")

    turns = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "model.completed":
                continue
            data = d.get("data", {})
            usage = data.get("usage", {})
            turns.append({
                "ts": d.get("ts", ""),
                "input": usage.get("input", 0),
                "output": usage.get("output", 0),
                "total": usage.get("total", 0),
                "compactions": data.get("compactionCount", 0),
                "promptCache": data.get("promptCache", {}),
                "messagesCount": len(data.get("messagesSnapshot", [])),
                "promptText_chars": len(data.get("finalPromptText", "")),
                "assistantText_chars": sum(len(t) for t in data.get("assistantTexts", [])),
            })

    if not turns:
        print("No model.completed events found.")
        return

    print(f"{'Turn':<6}{'Wall-time':<22}{'Input':>10}{'Output':>8}{'Total':>10}"
          f"{'Δ (s)':>9}{'Compact':>10}{'PromptText':>14}")
    print("─" * 95)
    prev_ts = None
    total_in, total_out = 0, 0
    for i, t in enumerate(turns, 1):
        ts_obj = datetime.fromisoformat(t["ts"].replace("Z", "+00:00"))
        delta = (ts_obj - prev_ts).total_seconds() if prev_ts else 0
        prev_ts = ts_obj
        total_in += t["input"]
        total_out += t["output"]
        print(f"{i:<6}{t['ts'][:19]:<22}{t['input']:>10,}{t['output']:>8,}"
              f"{t['total']:>10,}{delta:>9.0f}{t['compactions']:>10}"
              f"{t['promptText_chars']:>14,}")

    duration_s = (datetime.fromisoformat(turns[-1]["ts"].replace("Z", "+00:00")) -
                  datetime.fromisoformat(turns[0]["ts"].replace("Z", "+00:00"))).total_seconds()

    print("─" * 95)
    print(f"\n{'AGGREGATE':<6}{'':<22}{total_in:>10,}{total_out:>8,}{total_in+total_out:>10,}")
    print(f"\nWall-clock: {duration_s:.0f}s ({duration_s/60:.1f} min) across {len(turns)} model turns")
    print(f"Avg per turn: {total_in/len(turns):,.0f} input + {total_out/len(turns):,.0f} output")
    print(f"Peak input prompt: {max(t['input'] for t in turns):,} tokens (turn {1+max(range(len(turns)), key=lambda i: turns[i]['input'])})")
    print(f"Final answer output: {turns[-1]['output']:,} tokens")
    print(f"\nNote: 'input' = OpenClaw's accounting; LM Studio loaded context = 98,304.")
    print(f"      Values >98K imply OpenClaw compacted before sending OR includes uncompacted history.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        target = find_latest_trajectory()
    if not target or not target.exists():
        print(f"Trajectory not found: {target}", file=sys.stderr)
        sys.exit(1)
    analyze(target)
