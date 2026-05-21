"""Sync LM Studio's downloaded + loaded models into openclaw.json.

Runs `lms ls --json` (everything downloaded) and `lms ps --json` (currently
loaded with live contextLength). For each LLM (skipping embedding models),
upserts an entry under models.providers.lmstudio.models[] in openclaw.json
with sensible defaults — id, friendly name, vision/tool flags, contextWindow.

When a model is *currently loaded*, also writes contextTokens from the live
contextLength so OpenClaw's history budget matches what LM Studio is serving.

Idempotent — diffs against existing entries and only writes if something
actually changed. On change, sends SIGUSR1 to openclaw-gateway.service for
hot-reload (no full restart).

Usage:
    python sync_lm_models.py            # sync + maybe-reload
    python sync_lm_models.py --dry-run  # show diff, no write
    python sync_lm_models.py --no-reload # write but don't signal openclaw
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# V6 prep: source paths from the central _paths module at repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import LMS_EXE, OPENCLAW_JSON, WSL_DISTRO  # noqa: E402

# Static reasoning-effort scaffolding shared by all Qwen3 / Qwen3.6 variants.
# Other model families don't expose reasoning_effort so we only attach this for
# Qwen3.x architectures.
QWEN_REASONING = {
    "supportsReasoningEffort": True,
    "supportedReasoningEfforts": ["none", "minimal", "low", "medium", "high", "xhigh"],
    "reasoningEffortMap": {
        "off": "none", "none": "none", "minimal": "minimal",
        "low": "low",  "medium": "medium", "high": "high", "xhigh": "xhigh",
        "adaptive": "high", "max": "xhigh",
    },
}


def _lms_json(args: list[str]) -> list[dict]:
    """Run lms with --json and return parsed list. Empty list on any failure."""
    try:
        r = subprocess.run(
            [str(LMS_EXE), *args, "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            print(f"[sync] lms {args[0]} failed (rc={r.returncode}): {r.stderr.strip()[:200]}", file=sys.stderr)
            return []
        return json.loads(r.stdout)
    except FileNotFoundError:
        print(f"[sync] lms.exe not found at {LMS_EXE}", file=sys.stderr)
        return []
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        print(f"[sync] lms {args[0]} parse/timeout error: {e}", file=sys.stderr)
        return []


def _build_entry(lms_model: dict, loaded_ctx: int | None) -> dict:
    """Convert an lms model record → openclaw model entry."""
    arch = lms_model.get("architecture", "")
    vision = bool(lms_model.get("vision"))
    name = lms_model.get("displayName") or lms_model.get("modelKey", "?")
    params = lms_model.get("paramsString")
    if params and params not in name:
        name = f"{name} ({params})"
    if "(Local)" not in name:
        name = f"{name} (Local)"

    entry = {
        "id":   lms_model["modelKey"],
        "name": name,
        "reasoning": False,
        "input": ["text", "image"] if vision else ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "compat": {},
        "contextWindow": int(lms_model.get("maxContextLength", 32768)),
        "contextTokens": int(loaded_ctx if loaded_ctx else min(32768, lms_model.get("maxContextLength", 32768))),
        "maxTokens": 8192,
    }
    if "qwen3" in arch.lower() or "qwen35" in arch.lower():
        entry["compat"] = QWEN_REASONING
    return entry


def sync(dry_run: bool = False, no_reload: bool = False) -> dict:
    """Returns a summary dict: {added, updated, unchanged, removed}."""
    downloaded = [m for m in _lms_json(["ls"]) if m.get("type") == "llm"]
    loaded_map = {m["modelKey"]: m for m in _lms_json(["ps"]) if m.get("type") == "llm"}

    if not downloaded:
        return {"error": "no LLMs reported by lms ls — is LM Studio installed?"}

    cfg_path = OPENCLAW_JSON  # already a Path from _paths
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    lm_prov = (cfg.setdefault("models", {}).setdefault("providers", {})
                  .setdefault("lmstudio", {}))
    existing = {m["id"]: m for m in lm_prov.setdefault("models", []) if "id" in m}
    existing_ids = set(existing.keys())

    new_entries = []
    summary = {"added": [], "updated": [], "unchanged": [], "removed": []}

    for m in downloaded:
        key = m["modelKey"]
        loaded = loaded_map.get(key)
        loaded_ctx = loaded.get("contextLength") if loaded else None
        entry = _build_entry(m, loaded_ctx)

        if key in existing:
            old = existing[key]
            # Preserve user-edited maxTokens / reasoning if present
            if "maxTokens" in old:
                entry["maxTokens"] = old["maxTokens"]
            if old.get("reasoning") is not None:
                entry["reasoning"] = old["reasoning"]
            if old != entry:
                changes = [k for k in entry if old.get(k) != entry.get(k)]
                summary["updated"].append({"id": key, "changes": changes})
            else:
                summary["unchanged"].append(key)
        else:
            summary["added"].append(key)
        new_entries.append(entry)

    # Track removals (downloaded model deleted from LM Studio side)
    downloaded_ids = {m["modelKey"] for m in downloaded}
    for stale_id in existing_ids - downloaded_ids:
        summary["removed"].append(stale_id)

    lm_prov["models"] = new_entries

    # Also keep agents.defaults.models[<primary>] consistent
    primary = (cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", ""))
    if primary.startswith("lmstudio/"):
        bare = primary[len("lmstudio/"):]
        if bare not in downloaded_ids and downloaded_ids:
            # Primary points at a model no longer present — flag but don't auto-switch
            summary["primary_missing"] = primary

    changed = bool(summary["added"] or summary["updated"] or summary["removed"])
    if changed and not dry_run:
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        if not no_reload:
            try:
                # Use SIGUSR1 hot-reload (faster than full restart, preserves connections)
                subprocess.run(
                    ["wsl", "-d", WSL_DISTRO, "-u", "root", "--", "bash", "-c",
                     "systemctl kill -s SIGUSR1 openclaw-gateway.service"],
                    timeout=5, capture_output=True,
                )
                summary["reloaded"] = True
            except Exception as e:
                summary["reload_error"] = str(e)[:80]

    summary["dry_run"] = dry_run
    summary["changed"] = changed
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Show diff, don't write")
    ap.add_argument("--no-reload", action="store_true", help="Write but don't SIGUSR1 openclaw")
    args = ap.parse_args()

    result = sync(dry_run=args.dry_run, no_reload=args.no_reload)
    print(json.dumps(result, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
