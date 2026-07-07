"""Hot-reload env vars from .env.local without restarting the process (#27).

Vendored from sentinel-watchdog/sentinel_secrets/reload_env.py @ c9d42cf.
Pure stdlib + thread-safe by design — copying is preferred over a shared
package to avoid pulling watchdog's dep tree into the shared-brain bot.
Keep in sync.

Pattern:
  1. sentinel-secrets writes a new value to .env.local via /sync or /set
  2. The same handler fans out to every consumer's POST /internal/reload-env
  3. The consumer calls reload_env_in_process() — re-parses .env.local, diffs
     against current os.environ, pushes new values, fires registered
     hot-swap callbacks
  4. Caller receives a report:
       applied   = key changed AND a hot-swap callback ran successfully
                   → the process is fully live on the new value
       frozen    = key changed BUT no callback registered (or it raised)
                   → os.environ has the new value, but module-level
                     constants captured at import still hold the old
                     value. The container needs restart (use
                     /api/v2/secrets/{id}/restart_consumer from #26 Phase 4)
                     to fully propagate.
       unchanged = key's value matches os.environ already — no work

Honesty about what hot-swap can/can't do is the load-bearing UX choice.
A naive impl that just calls load_dotenv(override=True) and reports
"success" would mislead — most consumers cache env at import time, so
the runtime behaviour doesn't actually change without explicit per-key
swap logic.

This module is intentionally pure-stdlib + thread-safe. No external
deps so any consumer (watchdog v2, smdl, bridge.py, finance) can import
it without dragging in its sibling's dep tree.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Hot-swap callback registry ────────────────────────────────────────
# Each consumer registers callbacks for env keys that can swap live
# (typically those read at the call site rather than captured at import).
# Module-level globals captured at import are immutable from outside;
# you'd update them via a callback that runs `mod.SHARE_SECRET = v`.

_SWAP_REGISTRY: dict[str, Callable[[str], None]] = {}
_REGISTRY_LOCK = threading.RLock()


def register_hot_swap(env_key: str, callback: Callable[[str], None]) -> None:
    """Register `callback` to fire when reload-env detects env_key changed.
    The callback receives the new value as its single arg. Call this
    BEFORE the consumer starts serving requests."""
    with _REGISTRY_LOCK:
        _SWAP_REGISTRY[env_key] = callback


def clear_registry() -> None:
    """Test helper. Drops all registered callbacks."""
    with _REGISTRY_LOCK:
        _SWAP_REGISTRY.clear()


def registered_keys() -> list[str]:
    """Snapshot the keys that have hot-swap callbacks registered. Useful
    for the /internal/reload-env response so the UI can show which keys
    the consumer is capable of hot-swapping."""
    with _REGISTRY_LOCK:
        return sorted(_SWAP_REGISTRY.keys())


# ── .env.local parser ─────────────────────────────────────────────────
# Matches docker compose's env_file: directive semantics — KEY=VALUE
# per line, # comments, optional surrounding quotes. Intentionally
# minimal so it doesn't pull in python-dotenv.

def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a `.env`-style file → {KEY: VALUE}. Missing file → empty dict."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


# ── The main reload function ──────────────────────────────────────────

def reload_env_in_process(
    env_path: Path,
    *,
    keys: Optional[list[str]] = None,
) -> dict[str, list[str]]:
    """Re-parse `env_path`, push new values into os.environ, fire hot-swap
    callbacks where registered. Returns:

        {"applied": [...], "frozen": [...], "unchanged": [...],
         "missing": [...]}

    `applied`   — value changed AND hot-swap callback succeeded
    `frozen`    — value changed in os.environ but no callback registered
                  (or the callback raised). Restart needed to propagate
                  fully to module-level captures.
    `unchanged` — value matches os.environ already
    `missing`   — when `keys` was given, keys in that list NOT present in
                  the parsed .env.local

    When `keys` is given, restrict the operation to those keys only.
    """
    parsed = parse_env_file(env_path)
    if keys is not None:
        missing = [k for k in keys if k not in parsed]
        parsed = {k: v for k, v in parsed.items() if k in keys}
    else:
        missing = []

    applied: list[str] = []
    frozen: list[str] = []
    unchanged: list[str] = []

    for k, v in parsed.items():
        if os.environ.get(k) == v:
            unchanged.append(k)
            continue
        os.environ[k] = v
        with _REGISTRY_LOCK:
            cb = _SWAP_REGISTRY.get(k)
        if cb is not None:
            try:
                cb(v)
                applied.append(k)
                logger.info("reload-env: %s hot-swapped (callback applied)", k)
            except Exception:
                logger.exception("reload-env: %s callback failed; treating as frozen", k)
                frozen.append(k)
        else:
            frozen.append(k)
            logger.info("reload-env: %s pushed to os.environ; no callback (frozen at import)", k)

    return {
        "applied":   sorted(applied),
        "frozen":    sorted(frozen),
        "unchanged": sorted(unchanged),
        "missing":   sorted(missing),
    }
