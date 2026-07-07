"""Extract a benchmark entry from an OpenClaw session trajectory.

Given a trajectory file path and a turn range (e.g. 3..5 for the research
mission), pulls token usage / tool-call counts / wall-clock / compaction
data and emits a YAML entry ready to append to workspace/benchmarks/benchmarks.yaml.

Manual fields (task description, output file, quality assessment) are
left as templated placeholders for you to fill in.

Usage:
    python extract_benchmark.py <trajectory.jsonl> --from 3 --to 5 \\
        --task "V3 wishlist research" --output workspace/research/V3-handheld-AI-wishlist-sentinel.md \\
        --agent sentinel-local --model qwen/qwen3.6-27b

Or run with --help to see all flags.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)


def _yaml_dump(obj, indent=0):
    """Tiny YAML emitter (avoids pyyaml dep). Handles dict/list/scalar."""
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return f"{pad}{{}}"
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_yaml_dump(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return f"{pad}[]"
        return "\n".join(f"{pad}- {_scalar(x) if not isinstance(x, dict) else _yaml_dump(x, indent + 1).lstrip()}"
                         for x in obj)
    return f"{pad}{_scalar(obj)}"


def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\")
    if "\n" in s:
        # Block scalar
        return "|\n" + "\n".join("    " + line for line in s.splitlines())
    if any(c in s for c in ":#&*!|>'\"%@`") or s.startswith(("- ", "[", "{")):
        return f'"{s}"'
    return s


def _measure_output(path: Path) -> dict:
    """Word/char/line count + estimated page counts at 3 standard rates."""
    if not path.exists():
        return {"file_missing": str(path)}
    text = path.read_text(encoding="utf-8", errors="replace")
    words = len(text.split())
    chars = len(text)
    lines = text.count("\n") + 1
    return {
        "file": str(path),
        "words": words,
        "chars": chars,
        "lines": lines,
        "pages_double_spaced_250wpp": round(words / 250),
        "pages_single_spaced_500wpp": round(words / 500),
        "pages_published_750wpp":     round(words / 750),
    }


def extract(traj_path: Path, turn_from: int, turn_to: int) -> dict:
    """Pull metrics from a trajectory file's model.completed events in [turn_from, turn_to]."""
    turns = []
    with open(traj_path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "model.completed":
                turns.append(d)

    if turn_from < 1 or turn_to > len(turns):
        raise SystemExit(f"Invalid turn range {turn_from}-{turn_to}; trajectory has {len(turns)} model.completed events")

    sliced = turns[turn_from - 1:turn_to]
    if not sliced:
        raise SystemExit("No turns in selected range")

    # Aggregate token usage
    total_in = sum(t["data"].get("usage", {}).get("input", 0) for t in sliced)
    total_out = sum(t["data"].get("usage", {}).get("output", 0) for t in sliced)
    peak_in = max((t["data"].get("usage", {}).get("input", 0) for t in sliced), default=0)
    compaction_count = max((t["data"].get("compactionCount", 0) for t in sliced), default=0)

    # Wall-clock
    t0 = datetime.fromisoformat(sliced[0]["ts"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(sliced[-1]["ts"].replace("Z", "+00:00"))
    wall_seconds = round((t1 - t0).total_seconds())

    # Tool call counts — walk messagesSnapshot of last turn (cumulative view)
    tool_calls = Counter()
    last_snap = sliced[-1]["data"].get("messagesSnapshot", [])
    for msg in last_snap:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "toolCall":
                tool_calls[c.get("name", "?")] += 1

    return {
        "trajectory": str(traj_path),
        "turn_range": [turn_from, turn_to],
        "start_local": sliced[0]["ts"],
        "end_local": sliced[-1]["ts"],
        "wall_clock_seconds": wall_seconds,
        "wall_clock_minutes": round(wall_seconds / 60, 1),
        "model_invocations": len(sliced),
        "tokens": {
            "input": total_in,
            "output": total_out,
            "total": total_in + total_out,
            "peak_input_per_turn": peak_in,
            "ratio_in_to_out": round(total_in / max(total_out, 1), 1),
        },
        "tool_calls": dict(sorted(tool_calls.items(), key=lambda x: -x[1])),
        "compaction_count": compaction_count,
    }


def estimate_costs(tokens_in: int, tokens_out: int, wall_seconds: int) -> dict:
    """Energy at 300W avg + Singapore tariff. API-equivalent at typical mid-range pricing."""
    kwh = (300 / 1000) * (wall_seconds / 3600)
    sgd_electricity = round(kwh * 0.2727, 4)  # SG residential tariff
    # Rough mid-range API equivalent (Claude Sonnet / GPT-4o tier): $3 in, $15 out per 1M tokens
    usd_api = (tokens_in / 1_000_000 * 3.0) + (tokens_out / 1_000_000 * 15.0)
    sgd_api = round(usd_api * 1.35, 2)  # USD→SGD
    return {
        "electricity_sgd": sgd_electricity,
        "api_equivalent_sgd": sgd_api,
        "savings_sgd": round(sgd_api - sgd_electricity, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trajectory", help="Path to .trajectory.jsonl file")
    ap.add_argument("--from", dest="turn_from", type=int, required=True, help="First model.completed turn (1-indexed)")
    ap.add_argument("--to",   dest="turn_to",   type=int, required=True, help="Last model.completed turn (inclusive)")
    ap.add_argument("--task",   default="<DESCRIBE TASK>", help="What was asked of the agent")
    ap.add_argument("--output", default=None, help="Path to the produced artifact (markdown/file)")
    ap.add_argument("--agent",  default="<sentinel-local|claude-general-purpose|...>")
    ap.add_argument("--model",  default="qwen/qwen3.6-27b")
    ap.add_argument("--quantization", default="Q4_K_M")
    ap.add_argument("--context-loaded", type=int, default=98304)
    ap.add_argument("--id", default=None, help="Benchmark ID (default: <date>-<task-slug>)")
    args = ap.parse_args()

    traj = Path(args.trajectory)
    if not traj.exists():
        raise SystemExit(f"Trajectory not found: {traj}")

    metrics = extract(traj, args.turn_from, args.turn_to)
    cost = estimate_costs(metrics["tokens"]["input"], metrics["tokens"]["output"], metrics["wall_clock_seconds"])

    output_metrics = _measure_output(Path(args.output)) if args.output else {}

    bench_id = args.id or f"{metrics['start_local'][:10]}-" + re.sub(r"\W+", "-", args.task.lower()[:40]).strip("-")

    entry = {
        "id": bench_id,
        "task": args.task,
        "agent": args.agent,
        "model": args.model,
        "quantization": args.quantization,
        "context_loaded": args.context_loaded,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        **metrics,
        "output_artifact": output_metrics,
        "cost_estimate": cost,
        "quality_subjective_1to10": "<TODO>",
        "notes": "<TODO>",
    }

    print("# Append the following to workspace/benchmarks/benchmarks.yaml under `benchmarks:`\n")
    print("- " + _yaml_dump(entry, indent=1).lstrip())


if __name__ == "__main__":
    main()
