"""P6 — task-level telemetry for the browser assistant.

Reads the structured run log (`runs.jsonl`, written by agent_runner.log) and
rolls it up into the numbers the rollout gate asks for: success rate, status
mix, fence trips, vision usage, the approval rate, the grounding-method mix, and
duration percentiles. `runs.jsonl` already carries BOTH the per-run records (they
have a `status`) and the per-action gate decisions (label `gate-approved` /
`gate-denied` / `gate-declined`) — so one file is the single source.

Importable: `compute()` -> dict (the :8108 `/metrics` route serves this).
CLI:        `python metrics.py [--hours N]`  (pretty-prints the summary).
"""
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RUNLOG = os.path.join(HERE, "runs.jsonl")

# A run record has a "status"; gate records are labelled gate-*.
_GATE_LABELS = ("gate-approved", "gate-denied", "gate-declined")


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[k], 1)


def _read(path=RUNLOG):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def compute(hours: float | None = None, path: str = RUNLOG) -> dict:
    """Roll runs.jsonl into a metrics dict. `hours` limits to the trailing window."""
    rows = _read(path)
    cutoff = None
    if hours:
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - hours * 3600

    def _in_window(r):
        if cutoff is None:
            return True
        ts = _parse_ts(r.get("ts", ""))
        return ts is not None and ts.timestamp() >= cutoff

    runs = [r for r in rows if "status" in r and _in_window(r)]
    gates = [r for r in rows if r.get("label") in _GATE_LABELS and _in_window(r)]

    by_status: dict[str, int] = {}
    by_caller: dict[str, int] = {}   # cross-pillar attribution (panel / mcp / dove / a pillar)
    durs, steps = [], []
    vision = gated = 0
    ground = {"dom": 0, "fallback": 0}
    for r in runs:
        by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1
        _c = r.get("caller") or r.get("label") or "?"
        by_caller[_c] = by_caller.get(_c, 0) + 1
        if isinstance(r.get("dur_s"), (int, float)):
            durs.append(float(r["dur_s"]))
        if isinstance(r.get("steps"), int):
            steps.append(r["steps"])
        if r.get("use_vision"):
            vision += 1
        if r.get("gated"):
            gated += 1
        # ground-method mix: P5's grounder isn't built yet, so every run is DOM-index.
        ground["fallback" if r.get("ground") == "fallback" else "dom"] += 1

    total = len(runs)
    ok = by_status.get("ok", 0)
    approved = sum(1 for g in gates if g.get("label") == "gate-approved")
    denied = sum(1 for g in gates if g.get("label") in ("gate-denied", "gate-declined"))
    decisions = approved + denied

    first = min((_parse_ts(r.get("ts", "")) for r in runs if _parse_ts(r.get("ts", ""))), default=None)
    last = max((_parse_ts(r.get("ts", "")) for r in runs if _parse_ts(r.get("ts", ""))), default=None)

    return {
        "window_hours": hours,
        "runs": total,
        "success_rate": round(ok / total, 3) if total else None,
        "by_status": by_status,
        "by_caller": by_caller,
        "fenced": by_status.get("fenced_timeout", 0),
        "errors": by_status.get("error", 0),
        "gated_runs": gated,
        "vision_runs": vision,
        "ground_mix": ground,
        "dur_s": {"p50": _pct(durs, 50), "p95": _pct(durs, 95),
                  "max": round(max(durs), 1) if durs else None},
        "steps": {"avg": round(sum(steps) / len(steps), 1) if steps else None,
                  "max": max(steps) if steps else None},
        "approvals": {"approved": approved, "denied": denied,
                      "rate": round(approved / decisions, 3) if decisions else None},
        "first_run": first.isoformat() if first else None,
        "last_run": last.isoformat() if last else None,
    }


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Browser-assistant telemetry summary.")
    ap.add_argument("--hours", type=float, default=None, help="limit to the trailing N hours")
    args = ap.parse_args()
    print(json.dumps(compute(args.hours), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
