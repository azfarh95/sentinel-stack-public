#!/usr/bin/env python3
"""Sentinel app version discipline — ONE source of truth, zero drift.

Why this exists
---------------
Each Sentinel app can ship on multiple surfaces (a Tauri DESKTOP build and an
Android TWA/APK). Their versions kept drifting (e.g. watchdog desktop 0.2.7 vs
APK 0.2.5; finance twa-manifest 1.1.0 vs build.gradle "2"), which burned time
reconciling. This tool makes the version SINGLE-SOURCED per app and propagates
it everywhere, with a deterministic versionCode so Android upgrades are always
monotonic.

Convention (keep it disciplined; survives a move to native Kotlin)
------------------------------------------------------------------
  * versionName  = semver  "MAJOR.MINOR.PATCH"
  * versionCode  = MAJOR*10000 + MINOR*100 + PATCH   (deterministic, monotonic)
  * Canonical source per app:
      - if the app has a DESKTOP (Tauri) build, its src-tauri/tauri.conf.json
        `version` is canonical and the APK MIRRORS it (APK<->Desktop in sync).
      - otherwise the TWA's twa-manifest.json `appVersionName` is canonical.
  * TWA targets written: twa-manifest.json (appVersionName/appVersionCode) AND
    app/build.gradle (versionName/versionCode), so the repo is internally
    consistent and a plain `gradlew assembleRelease` builds the right version
    WITHOUT bubblewrap's interactive update prompt.
  * Native-Kotlin target (kind="kotlin"): app/build.gradle.kts versionName/Code.

Usage
-----
  python sync_app_versions.py            # --check: report every app's drift
  python sync_app_versions.py --apply              # fix ALL apps to canonical
  python sync_app_versions.py --apply --app sentinel-watchdog   # one app

After --apply on a TWA, build with the repo's documented bubblewrap flow
(`bubblewrap update` then the signed `bubblewrap build`), then publish with
regen_manifest.py. This tool only sets versions; it never builds or signs.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HOME = Path("C:/Users/azfar")

# ── registry ────────────────────────────────────────────────────────────────
# kind: "twa" (bubblewrap) | "kotlin" (native). desktop_tauri = canonical source
# when set (APK mirrors desktop). Add the kotlin build.gradle.kts here on migration.
APPS = [
    {
        "id": "sentinel-watchdog",                      # hub app_id
        "package": "xyz.az_sentinel.watchdog",
        "kind": "twa",
        "twa_repo": HOME / "sentinel-watchdog-twa",
        # APK is locked to the desktop's version (the pair the owner called out)
        "desktop_tauri": HOME / "sentinel-watchdog/admin/src-tauri/tauri.conf.json",
    },
    {
        "id": "sentinel-finance",
        "package": "xyz.az_sentinel.finance",
        "kind": "twa",
        "twa_repo": HOME / "sentinel-finance-twa",
        "desktop_tauri": None,                          # no desktop -> manifest is canonical
    },
    {
        "id": "sentinel-suite",
        "package": "xyz.az_sentinel.suite",
        "kind": "twa",
        "twa_repo": HOME / "sentinel-suite-twa",
        # NOTE: a suite desktop exists (sentinel-suite-desktop 1.0.0) but there are
        # TWO redundant suite-desktop repos — left uncoupled until that's resolved.
        "desktop_tauri": None,
    },
]


def version_code(semver: str) -> int:
    """MAJOR*10000 + MINOR*100 + PATCH. Deterministic + monotonic across bumps."""
    parts = re.findall(r"\d+", semver)
    while len(parts) < 3:
        parts.append("0")
    major, minor, patch = (int(parts[0]), int(parts[1]), int(parts[2]))
    return major * 10000 + minor * 100 + patch


def norm_semver(raw: str) -> str:
    """Normalise a loose version ('1', '1.1', '0.2.7') to MAJOR.MINOR.PATCH."""
    parts = re.findall(r"\d+", raw or "0")
    while len(parts) < 3:
        parts.append("0")
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def canonical_version(app: dict) -> str:
    dt = app.get("desktop_tauri")
    if dt and Path(dt).exists():
        return norm_semver(_read_json(Path(dt)).get("version", "0.0.0"))
    man = app["twa_repo"] / "twa-manifest.json"
    if man.exists():
        return norm_semver(_read_json(man).get("appVersionName", "0.0.0"))
    return "0.0.0"


def current_state(app: dict) -> dict:
    repo = app["twa_repo"]
    man_p = repo / "twa-manifest.json"
    gradle_p = repo / "app" / "build.gradle"
    st = {"manifest_name": None, "manifest_code": None,
          "gradle_name": None, "gradle_code": None,
          "desktop": None, "start_url": None}
    if man_p.exists():
        m = _read_json(man_p)
        st["manifest_name"] = m.get("appVersionName")
        st["manifest_code"] = m.get("appVersionCode")
        st["start_url"] = m.get("startUrl")
    if gradle_p.exists():
        g = gradle_p.read_text(encoding="utf-8")
        nm = re.search(r'versionName\s+"([^"]+)"', g)
        cd = re.search(r"versionCode\s+(\d+)", g)
        st["gradle_name"] = nm.group(1) if nm else None
        st["gradle_code"] = int(cd.group(1)) if cd else None
    dt = app.get("desktop_tauri")
    if dt and Path(dt).exists():
        st["desktop"] = _read_json(Path(dt)).get("version")
    return st


def apply_app(app: dict) -> list[str]:
    """Write canonical version into all of the app's targets. Returns change log."""
    ver = canonical_version(app)
    code = version_code(ver)
    repo = app["twa_repo"]
    changes: list[str] = []

    # monotonic safety: never let the derived code regress below what's shipped
    st = current_state(app)
    shipped = max([c for c in (st["manifest_code"], st["gradle_code"]) if c] or [0])
    if code < shipped:
        code = shipped + 1
        changes.append(f"  ! derived code < shipped {shipped}; bumped to {code}")

    if app["kind"] == "twa":
        man_p = repo / "twa-manifest.json"
        m = _read_json(man_p)
        m["appVersionName"] = ver
        m["appVersionCode"] = code
        man_p.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        changes.append(f"  twa-manifest.json -> {ver} (code {code})")

        gradle_p = repo / "app" / "build.gradle"
        if gradle_p.exists():
            g = gradle_p.read_text(encoding="utf-8")
            g2 = re.sub(r'versionName\s+"[^"]+"', f'versionName "{ver}"', g)
            g2 = re.sub(r"versionCode\s+\d+", f"versionCode {code}", g2)
            if g2 != g:
                gradle_p.write_text(g2, encoding="utf-8")
                changes.append(f"  app/build.gradle -> versionName {ver} / versionCode {code}")
    elif app["kind"] == "kotlin":
        gk = repo / "app" / "build.gradle.kts"
        if gk.exists():
            g = gk.read_text(encoding="utf-8")
            g2 = re.sub(r'versionName\s*=\s*"[^"]+"', f'versionName = "{ver}"', g)
            g2 = re.sub(r"versionCode\s*=\s*\d+", f"versionCode = {code}", g2)
            if g2 != g:
                gk.write_text(g2, encoding="utf-8")
                changes.append(f"  app/build.gradle.kts -> {ver} (code {code})")
    return changes or ["  (already in sync)"]


def check() -> int:
    drift = 0
    print(f"{'app':22} {'canonical':10} {'manifest':16} {'gradle':16} {'desktop':9}  status")
    print("-" * 92)
    for app in APPS:
        ver = canonical_version(app)
        code = version_code(ver)
        st = current_state(app)
        man = f"{st['manifest_name']}/{st['manifest_code']}"
        grd = f"{st['gradle_name']}/{st['gradle_code']}"
        ok = (st["manifest_name"] == ver and st["manifest_code"] == code
              and st["gradle_name"] == ver and st["gradle_code"] == code)
        status = "OK" if ok else "DRIFT"
        if not ok:
            drift += 1
        print(f"{app['id']:22} {ver:>9}={code:<5} {man:16} {grd:16} "
              f"{str(st['desktop']):9}  {status}")
    print("-" * 92)
    print(f"target code = MAJOR*10000+MINOR*100+PATCH | {drift} app(s) drifted"
          + ("" if drift else " — all in sync"))
    return drift


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel app version sync")
    ap.add_argument("--apply", action="store_true", help="write canonical versions")
    ap.add_argument("--app", help="limit to one app id")
    args = ap.parse_args()
    if not args.apply:
        return 0 if check() == 0 else 1
    targets = [a for a in APPS if not args.app or a["id"] == args.app]
    if not targets:
        print(f"no app id '{args.app}'"); return 2
    for app in targets:
        print(f"{app['id']} -> canonical {canonical_version(app)}:")
        for line in apply_app(app):
            print(line)
    print("\nre-checking:")
    return 0 if check() == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
