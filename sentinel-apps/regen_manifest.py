#!/usr/bin/env python3
"""Regenerate sentinel-apps/manifest.json from the artifacts actually on disk.

The Suite "Apps" hub (sentinel-vpn-dashboard /apps) reads this manifest. It
used to be hand-bumped by per-project build scripts and drifted out of sync
(versions on disk weren't listed). This script is the single source of truth:
it scans every <app_id>/<version>/<file>, computes real size + sha256, and
emits a complete manifest — while PRESERVING any human-authored per-version
metadata (changelog, released date, min_sdk, target_sdk) from the existing
manifest. Re-run after dropping a new artifact in.

    py regen_manifest.py            # rewrite manifest.json from on-disk artifacts
    py regen_manifest.py --check    # print what it WOULD write, don't touch disk
    py regen_manifest.py --ingest   # first pull newest builds from build_artifacts.yaml
                                     # into the store (+ code-sign if SENTINEL_SIGN_THUMBPRINT
                                     # is set), then rewrite manifest.json

Honesty notes:
- size_bytes + sha256 are always recomputed from the file (never trusted).
- `released` falls back to the file's mtime date when not already recorded.
- New versions with no recorded changelog get a neutral placeholder.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"

# ── Auto-ingest (--ingest) ───────────────────────────────────────────────────
# Pull the newest built installers straight from each repo's output dirs (the
# SoT `build_artifacts.yaml`) into the store, so "publish the latest builds" is
# one command instead of hand-copying. Windows installers (.msi/.exe) land in
# the mapped app's latest version dir, clean-renamed and (optionally) code-signed.
# APK version bumps are flagged, not auto-bumped (they need a fresh version dir).
BUILD_ARTIFACTS = Path(os.environ.get(
    "SENTINEL_BUILD_ARTIFACTS",
    r"C:\Users\azfar\sentinel-watchdog\shared\build_artifacts.yaml"))
# Signing is opt-in: set SENTINEL_SIGN_THUMBPRINT to a cert thumbprint in the
# CurrentUser\My store and ingested .msi/.exe get signtool-signed + timestamped.
SIGN_THUMBPRINT = os.environ.get("SENTINEL_SIGN_THUMBPRINT", "")
SIGN_TIMESTAMP = os.environ.get("SENTINEL_SIGN_TIMESTAMP", "http://timestamp.digicert.com")
# build_artifacts.yaml id -> (store app id, clean filename base)
INGEST_MAP = {
    "sentinel-admin":       ("sentinel-watchdog", "SentinelWatchdog"),
    "sentinel-network":     ("sentinel-network",  "SentinelNetwork"),
    "sentinel-suite-twa":   ("sentinel-suite",    "SentinelSuite"),
    "sentinel-finance-twa": ("sentinel-finance",  "SentinelFinance"),
    "sentinel-smdl-twa":    ("smdl-tv",           "SentinelMediaTV"),
    "sentinel-ai-mobile":   ("sentinel-ai-mobile", "SentinelAIMobile"),
}

# Per-app display metadata. Keyed by directory name under sentinel-apps/.
# Adding a new app = drop its artifacts in <id>/<version>/ and add a row here.
REGISTRY = {
    "smdl-iptv": {
        "name": "SMDL IPTV", "icon": "📺", "package": "com.azsentinel.smdliptv",
        "category": "media",
        "description": "Netflix-style IPTV browser — 11k+ free public channels "
                       "(iptv-org, Free-TV, i.mjh.nz + community feeds). EPG, "
                       "recording, geo-block warnings, VLC/inline playback.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/tree/main/android",
        "homepage": "https://media.your-domain.example.com/iptv",
    },
    "sentinel-finance": {
        "name": "Sentinel Finance", "icon": "💰", "package": "xyz.az_sentinel.finance",
        "category": "finance",
        "description": "Net worth, bank statements, reconciliation, cash forecast "
                       "and planning — the Sentinel Finance dashboard.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-finance-twa",
        "homepage": "https://sentinelfinance.your-domain.example.com/",
    },
    "sentinel-watchdog": {
        "name": "Sentinel Watchdog", "icon": "🛡️", "package": "xyz.az_sentinel.watchdog",
        "category": "ops",
        "description": "Service-health ops console — probes, status, restart, "
                       "and incident view across every Sentinel pillar.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-watchdog-twa",
        "homepage": "https://watchdog.your-domain.example.com/miniapp",
    },
    "sentinel-suite": {
        "name": "Sentinel Suite", "icon": "🛡", "package": "xyz.az_sentinel.suite",
        "category": "launcher",
        "description": "The owner-only launcher — one tap to every Sentinel "
                       "pillar plus this Apps store.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-suite-twa",
        "homepage": "https://suite.your-domain.example.com/",
    },
    "sentinel-network": {
        "name": "Sentinel Network", "icon": "🌐", "package": "com.azsentinel.network",
        "category": "network",
        "description": "Owner-only tailnet control plane — Headscale nodes, "
                       "pre-auth keys, routes/exit nodes, ACL, AmneziaWG and WoL. "
                       "Tailnet is the security boundary.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-network",
        "homepage": "https://network.your-domain.example.com/",
    },
    "smdl-tv": {
        "name": "Sentinel Media TV", "icon": "🎬", "package": "com.azsentinel.smdltv",
        "category": "media",
        "description": "The public community / Play TV build — IPTV browser over "
                       "free public channels with EPG and inline playback. Wraps "
                       "tv.sentinelsuite.xyz; licensing/entitlements enforced "
                       "server-side. Distinct from the owner-only IPTV app.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-smdl/tree/main/android-twa",
        "homepage": "https://tv.sentinelsuite.xyz/iptv",
    },
    "sentinel-home": {
        "name": "Sentinel Home", "icon": "🏠", "package": "xyz.azsentinel.home",
        "category": "home",
        "description": "Owner-only smart-home control + geofencing — Xiaomi, Tuya "
                       "and Tapo devices via Home Assistant, with Google sign-in "
                       "and location-based automation (e.g. turn on the aircon as "
                       "you near home). Native Flutter; talks only to home.svc.",
        "homepage": "https://home.svc.your-domain.example.com/",
    },
    "sentinel-translate-keyboard": {
        "name": "Sentinel Translate Keyboard", "icon": "🌐", "package": "xyz.azsentinel.translatekeyboard",
        "category": "tools",
        "description": "Private translate keyboard + on-screen translator. The keyboard "
                       "translates the field you type (one tap) and an on-screen 🌐 bubble "
                       "translates what's already on screen — incoming chat messages, etc. "
                       "Translation runs on-device (Google ML Kit, ~59 languages, offline "
                       "after pack download); target follows your phone language. Reads "
                       "screen text via Accessibility (any script incl. Russian) with an OCR "
                       "fallback. Optional self-hosted LibreTranslate server engine. No cloud "
                       "API key.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-translate-keyboard",
        "homepage": "https://translate.svc.your-domain.example.com/",
    },
    "sentinel-ai-mobile": {
        "name": "Volery", "icon": "🐦", "package": "xyz.azsentinel.aimobile",
        "category": "ai",
        "description": "Your private AI flock. Scout runs entirely on your phone — offline, "
                       "no account, no cloud. Sage connects to a bigger model you bring: your "
                       "own computer over Tailscale (LM Studio / Ollama / llama.cpp) or any "
                       "OpenAI-compatible cloud key. Dove is the hive (memory + tools, advanced). "
                       "Your data stays on your devices. Your suite. Your AI.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/sentinel-ai-mobile",
        "homepage": "https://suite.your-domain.example.com/apps",
    },
}

# Extra per-version metadata kept even though it isn't derivable from the file.
DEFAULT_MIN_SDK, DEFAULT_TARGET_SDK = 26, 34

# A version dir may now hold more than one artifact (the Android APK plus
# Windows desktop installers). Classify by extension so the hub can offer a
# download button per platform. (platform, kind, human label).
_ARTIFACT_KINDS = {
    ".apk": ("android", "apk", "Android APK"),
    ".msi": ("windows", "msi", "Windows installer (MSI)"),
    ".exe": ("windows", "nsis", "Windows installer (.exe)"),
}


def _classify(path: Path):
    return _ARTIFACT_KINDS.get(path.suffix.lower(),
                               ("other", path.suffix.lstrip(".") or "bin",
                                path.name))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _version_sort_key(v: str):
    """Sort 0.2.10 > 0.2.9 > 0.2.2 and '1.1.0' > '1'. Non-numeric → 0."""
    parts = []
    for p in str(v).lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return parts


def _load_existing() -> dict:
    if not MANIFEST.is_file():
        return {}
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # Index existing per-version metadata by (app_id, version) for preservation.
    out = {}
    for app in data.get("apps", []):
        for ver in app.get("versions", []):
            out[(app["id"], str(ver.get("version")))] = ver
    return out


def _find_signtool() -> "str | None":
    cands = sorted((Path(r"C:\Program Files (x86)\Windows Kits\10\bin")).glob("*/x64/signtool.exe"),
                   reverse=True)
    return str(cands[0]) if cands else None


def _sign(path: Path) -> bool:
    """signtool-sign + RFC3161-timestamp a file with SIGN_THUMBPRINT (opt-in)."""
    if not SIGN_THUMBPRINT:
        return False
    st = _find_signtool()
    if not st:
        print("  ! signtool not found — skipping sign", file=sys.stderr)
        return False
    r = subprocess.run([st, "sign", "/sha1", SIGN_THUMBPRINT, "/fd", "SHA256",
                        "/tr", SIGN_TIMESTAMP, "/td", "SHA256", str(path)],
                       capture_output=True, text=True)
    ok = r.returncode == 0
    print(f"    {'signed' if ok else 'SIGN FAILED'}: {path.name}"
          + ("" if ok else f" :: {(r.stderr or r.stdout).strip()[:140]}"), file=sys.stderr)
    return ok


def _latest_version_dir(app_id: str) -> "Path | None":
    app = HERE / app_id
    if not app.is_dir():
        return None
    vdirs = [p for p in app.iterdir() if p.is_dir()]
    return max(vdirs, key=lambda d: _version_sort_key(d.name)) if vdirs else None


def ingest() -> None:
    """Copy the newest built installers from build_artifacts.yaml output dirs
    into the store (then build() picks them up). Windows installers only —
    APK version bumps are reported, not auto-added (they need a new version dir)."""
    if not BUILD_ARTIFACTS.is_file():
        print(f"  ! build_artifacts.yaml not found at {BUILD_ARTIFACTS}", file=sys.stderr)
        return
    import yaml  # local import: only needed for --ingest
    cat = yaml.safe_load(BUILD_ARTIFACTS.read_text(encoding="utf-8")) or {}
    print(f"ingest: reading {BUILD_ARTIFACTS}"
          + (f"  (signing with {SIGN_THUMBPRINT[:12]}…)" if SIGN_THUMBPRINT else "  (no signing)"),
          file=sys.stderr)
    for art in cat.get("artifacts", []):
        bid = art.get("id")
        if bid not in INGEST_MAP:
            continue
        store_id, base = INGEST_MAP[bid]
        base_dir = Path(art.get("base_dir", ""))
        newest: dict[str, Path] = {}  # ext -> newest matching file
        for g in art.get("globs", []):
            for m in base_dir.glob(g):
                ext = m.suffix.lower()
                if m.is_file() and ext in (".msi", ".exe", ".apk"):
                    if ext not in newest or m.stat().st_mtime > newest[ext].stat().st_mtime:
                        newest[ext] = m
        if not newest:
            continue
        vdir = _latest_version_dir(store_id)
        if vdir is None:
            print(f"  ! {store_id}: no version dir in store — skipping {bid}", file=sys.stderr)
            continue
        for ext, src in newest.items():
            if ext == ".apk":
                cur = next(iter(vdir.glob("*.apk")), None)
                if cur is None or _sha256(cur) != _sha256(src):
                    print(f"  ~ {store_id}: new APK available ({src.name}) — add a new "
                          f"version dir manually; not auto-bumped", file=sys.stderr)
                continue
            ver_m = re.search(r"(\d+\.\d+\.\d+)", src.name)
            ver = ver_m.group(1) if ver_m else "0.0.0"
            dest = vdir / (f"{base}-{ver}-x64-setup.exe" if ext == ".exe"
                           else f"{base}-{ver}-x64.msi")
            if dest.exists() and _sha256(dest) == _sha256(src):
                print(f"  = {store_id}/{vdir.name}/{dest.name} (unchanged)", file=sys.stderr)
                continue
            shutil.copy2(src, dest)
            print(f"  + {store_id}/{vdir.name}/{dest.name}", file=sys.stderr)
            _sign(dest)


def build() -> dict:
    existing = _load_existing()
    apps = []
    for app_dir in sorted(p for p in HERE.iterdir() if p.is_dir()):
        app_id = app_dir.name
        reg = REGISTRY.get(app_id)
        if reg is None:
            print(f"  ! skipping unknown app dir (add to REGISTRY): {app_id}",
                  file=sys.stderr)
            continue
        versions = []
        for ver_dir in sorted((p for p in app_dir.iterdir() if p.is_dir()),
                              key=lambda d: _version_sort_key(d.name), reverse=True):
            files = sorted(f for f in ver_dir.rglob("*") if f.is_file())
            if not files:
                continue
            artifacts = []
            for f in files:
                platform, kind, label = _classify(f)
                artifacts.append({
                    "platform": platform, "kind": kind, "label": label,
                    "file": f"{ver_dir.name}/{f.relative_to(ver_dir).as_posix()}",
                    "size_bytes": f.stat().st_size,
                    "sha256": _sha256(f),
                })
            # Primary = the APK if present (preserves the install/update flow and
            # the legacy top-level file/size/sha256 fields), else the first file.
            primary = next((a for a in artifacts if a["kind"] == "apk"), artifacts[0])
            ver = ver_dir.name.lstrip("v")
            prev = existing.get((app_id, ver), {})
            mtime = _dt.date.fromtimestamp(
                (ver_dir / primary["file"].split("/", 1)[1]).stat().st_mtime
            ).isoformat()
            entry = {
                "version": ver,
                "released": prev.get("released") or mtime,
                "file": primary["file"],
                "size_bytes": primary["size_bytes"],
                "sha256": primary["sha256"],
                "min_sdk": prev.get("min_sdk", DEFAULT_MIN_SDK),
                "target_sdk": prev.get("target_sdk", DEFAULT_TARGET_SDK),
                "changelog": prev.get("changelog") or f"Build {ver}.",
                "artifacts": artifacts,
            }
            if "code" in prev:
                entry["code"] = prev["code"]
            versions.append(entry)
        if not versions:
            continue
        apps.append({
            "id": app_id, **{k: reg[k] for k in
                             ("name", "icon", "package", "category", "description")},
            "repo": reg.get("repo"), "homepage": reg.get("homepage"),
            "latest": versions[0]["version"],
            "versions": versions,
        })
    return {
        "_comment": "Sentinel Apps catalogue — owner-only sideload distribution. "
                    "Regenerated by regen_manifest.py from on-disk artifacts; "
                    "per-version changelog/metadata is preserved across runs.",
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "apps": apps,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="print the manifest that would be written; don't touch disk")
    ap.add_argument("--ingest", action="store_true",
                    help="first pull newest built installers from build_artifacts.yaml "
                         "into the store (set SENTINEL_SIGN_THUMBPRINT to also code-sign), "
                         "then regenerate")
    args = ap.parse_args()
    if args.ingest:
        ingest()
    manifest = build()
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        # Windows consoles are often cp1252; the emoji icons would crash a naive
        # print. Write through the buffer as UTF-8 with replacement so --check
        # works everywhere.
        sys.stdout.buffer.write(text.encode("utf-8", "replace"))
        return 0
    MANIFEST.write_text(text, encoding="utf-8")
    n_apps = len(manifest["apps"])
    n_vers = sum(len(a["versions"]) for a in manifest["apps"])
    print(f"wrote {MANIFEST}  ({n_apps} apps, {n_vers} versions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
