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


# Pinned context for the llama-swap-managed Qwen lane. llama-swap idle-unloads
# the model (ttl), and a bare GET /props does NOT route through llama-swap (its
# native endpoints live at /upstream/:model/props) — so we must NOT probe to
# derive n_ctx, or we'd either get 404→32768 (the AI-002 footgun) or, worse,
# respawn the 22 GB model every 5-min sync and defeat the TTL-unload. The
# llama-swap config (C:\Users\azfar\llama-swap\config.yaml, locked -c 65536) is
# the source of truth; mirror it here. Only used when nothing is loaded.
LLAMA_SWAP_PINNED_CTX = 65536


def _active_server_ctx() -> int | None:
    """Loaded context (n_ctx) from the active OpenAI-compatible backend on :1234.

    Backend-aware, and llama-swap-safe:
      * If llama-swap fronts :1234 (its /running endpoint answers), read the
        LIVE n_ctx straight from the loaded model's backend proxy — but ONLY
        when a model is already `ready`. If nothing is loaded (TTL-unloaded),
        return the pinned value rather than spawning the 22 GB model just to
        read a number (that would defeat the idle-unload). This keeps the
        AI-002 contextTokens honest without the 32768 footgun.
      * Otherwise (LM Studio rollback / direct llama-server) fall back to the
        original bare /props poll.
    Returns None if unreachable (then we keep the old LM-Studio-only behaviour)."""
    import urllib.request

    # 1) llama-swap path — /running lists models WITHOUT loading any.
    try:
        with urllib.request.urlopen("http://127.0.0.1:1234/running", timeout=3) as r:
            running = (json.loads(r.read().decode()) or {}).get("running", [])
        for m in running:
            if m.get("state") == "ready" and m.get("proxy"):
                try:
                    with urllib.request.urlopen(m["proxy"].rstrip("/") + "/props", timeout=3) as r2:
                        d = json.loads(r2.read().decode())
                    nctx = (d.get("default_generation_settings") or {}).get("n_ctx") or d.get("n_ctx")
                    if nctx:
                        return int(nctx)
                except Exception:
                    pass
        return LLAMA_SWAP_PINNED_CTX  # behind llama-swap but idle/unloaded
    except Exception:
        pass

    # 2) Not llama-swap — original behaviour: bare /props on :1234.
    try:
        with urllib.request.urlopen("http://127.0.0.1:1234/props", timeout=3) as r:
            d = json.loads(r.read().decode())
        nctx = (d.get("default_generation_settings") or {}).get("n_ctx") or d.get("n_ctx")
        return int(nctx) if nctx else None
    except Exception:
        return None


def _build_entry(lms_model: dict, loaded_ctx: int | None, server_ctx: int | None = None) -> dict:
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
        # Fallback when BOTH live reads (lms ps + backend /props) are momentarily
        # unavailable — e.g. the 5-min tick lands inside llama-server's ~2-min
        # cold-load window, when llama-swap's /running is briefly unreachable and
        # _active_server_ctx() returns None. The generic min(32768,…) default is the
        # AI-002 footgun (it under-sizes the qwen lane → OpenClaw history budget
        # collapses → "Context overflow" on the next turn). For the llama-swap-
        # managed qwen lane the source of truth is the LOCKED config (-c 65536), so
        # pin the fallback to it; other models keep the conservative 32768 default.
        "contextTokens": int(
            loaded_ctx or server_ctx
            or (LLAMA_SWAP_PINNED_CTX
                if str(lms_model.get("modelKey", "")).startswith("qwen/qwen3.6")
                else min(32768, lms_model.get("maxContextLength", 32768)))),
        "maxTokens": 8192,
    }
    if "qwen3" in arch.lower() or "qwen35" in arch.lower():
        entry["compat"] = QWEN_REASONING
    return entry


def sync(dry_run: bool = False, no_reload: bool = False) -> dict:
    """Returns a summary dict: {added, updated, unchanged, removed}."""
    downloaded = [m for m in _lms_json(["ls"]) if m.get("type") == "llm"]
    loaded_map = {m["modelKey"]: m for m in _lms_json(["ps"]) if m.get("type") == "llm"}
    # Live context from the active backend (llama-server :1234) — used when LM
    # Studio reports nothing loaded so we don't fall back to the 32768 default.
    server_ctx = _active_server_ctx()

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
        entry = _build_entry(m, loaded_ctx, server_ctx)

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
