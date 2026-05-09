#!/usr/bin/env bash
# patch_openclaw_pairing.sh
# Applies (or rolls back) the custom pairing message in OpenClaw's dist.
#
# Usage:
#   ./patch_openclaw_pairing.sh          — apply patch
#   ./patch_openclaw_pairing.sh rollback — restore original
#
# Safe to re-run: detects if already patched/original and skips accordingly.
# Re-apply after: npm update -g openclaw

set -euo pipefail

# Resolve the real user's home (survives sudo re-invocation where $HOME becomes /root)
REAL_HOME="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)"
DIST_FILE="$REAL_HOME/.npm-global/lib/node_modules/openclaw/dist/pairing-messages-os97WTVG.js"

# Re-invoke with sudo if the dist file isn't writable by current user
if [[ ! -w "$DIST_FILE" ]] && [[ "$EUID" -ne 0 ]]; then
    echo "Dist file not writable — re-running with sudo..."
    exec sudo bash "$0" "$@"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL="$SCRIPT_DIR/patches/pairing-messages-os97WTVG.js.original"
PATCHED="$SCRIPT_DIR/patches/pairing-messages-os97WTVG.js.patched"
BACKUP="$SCRIPT_DIR/patches/pairing-messages-os97WTVG.js.backup"

# ── Sanity checks ──────────────────────────────────────────────────────────────

if [[ ! -f "$DIST_FILE" ]]; then
    echo "ERROR: Dist file not found: $DIST_FILE"
    echo "  Is OpenClaw installed? Try: npm install -g openclaw"
    exit 1
fi

if [[ ! -f "$ORIGINAL" ]] || [[ ! -f "$PATCHED" ]]; then
    echo "ERROR: Patch files missing from $SCRIPT_DIR/patches/"
    echo "  Expected: pairing-messages-os97WTVG.js.original"
    echo "            pairing-messages-os97WTVG.js.patched"
    exit 1
fi

# ── Detect current state ───────────────────────────────────────────────────────

is_original() { diff -q "$DIST_FILE" "$ORIGINAL" > /dev/null 2>&1; }
is_patched()  { diff -q "$DIST_FILE" "$PATCHED"  > /dev/null 2>&1; }

# ── Rollback ───────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "rollback" ]]; then
    if is_original; then
        echo "Already at original — nothing to roll back."
        exit 0
    fi
    if [[ -f "$BACKUP" ]]; then
        cp "$BACKUP" "$DIST_FILE"
        echo "Rolled back from backup: $BACKUP"
    else
        cp "$ORIGINAL" "$DIST_FILE"
        echo "Rolled back to original (no backup found, used .original)."
    fi
    exit 0
fi

# ── Apply patch ────────────────────────────────────────────────────────────────

if is_patched; then
    echo "Already patched — nothing to do."
    exit 0
fi

if ! is_original; then
    echo "WARNING: Dist file differs from both original and patched versions."
    echo "  OpenClaw may have been updated. Review before patching:"
    echo "  $DIST_FILE"
    echo ""
    echo "  Diff vs original:"
    diff "$ORIGINAL" "$DIST_FILE" || true
    echo ""
    read -rp "Proceed anyway? (y/N): " confirm
    [[ "${confirm,,}" == "y" ]] || { echo "Aborted."; exit 1; }
fi

# Back up current state before touching anything
cp "$DIST_FILE" "$BACKUP"
echo "Backed up current dist to: $BACKUP"

cp "$PATCHED" "$DIST_FILE"
echo "Patch applied: $DIST_FILE"
echo ""
echo "To roll back: ./patch_openclaw_pairing.sh rollback"
