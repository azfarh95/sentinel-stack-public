"""Sentinel Finance — daily backup of configs + Firefly data.

Output: /data/backups/sentinel-finance-YYYY-MM-DD.tar.gz containing
  finance/*.yaml (copied verbatim from /finance)
  firefly/accounts.json
  firefly/transactions.json (paginated full export)
  firefly/categories.json
  firefly/tags.json
  firefly/budgets.json
  manifest.json (timestamps, counts, versions)

Retention: 7 days. Older archives are pruned.

The 02:00 scheduler invokes `run_backup()`. Manual trigger via the
backup_now() MCP tool or POST /config/backup/run (admin only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tarfile
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
FINANCE_DIR = Path("/finance")
BACKUP_DIR = Path("/data/backups")
RETENTION_DAYS = 7


def _pat() -> str:
    return os.environ.get("FIREFLY_PAT", "")


async def _fetch_paginated(client: httpx.AsyncClient, path: str) -> list:
    """GET /api/v1/<path>, follow pagination, return concatenated `data` list."""
    pat = _pat()
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    out: list = []
    page = 1
    while True:
        r = await client.get(
            f"{FIREFLY_URL}/api/v1/{path}",
            headers=headers,
            params={"limit": 200, "page": page},
            timeout=60,
        )
        if r.status_code != 200:
            logger.warning("firefly %s page %d returned %d", path, page, r.status_code)
            return out
        body = r.json()
        out.extend(body.get("data", []))
        meta = body.get("meta", {}).get("pagination", {})
        total_pages = int(meta.get("total_pages", 1) or 1)
        if page >= total_pages:
            break
        page += 1
    return out


async def _dump_firefly(work: Path) -> dict:
    """Hit Firefly's REST API and save JSON files. Returns counts."""
    ff_dir = work / "firefly"
    ff_dir.mkdir(parents=True, exist_ok=True)
    counts: dict = {}
    if not _pat():
        logger.warning("FIREFLY_PAT missing — skipping Firefly export")
        return counts
    async with httpx.AsyncClient() as c:
        for endpoint in ("accounts", "transactions", "categories", "tags", "budgets"):
            try:
                data = await _fetch_paginated(c, endpoint)
                (ff_dir / f"{endpoint}.json").write_text(json.dumps(data, indent=2))
                counts[endpoint] = len(data)
                logger.info("firefly %s: %d rows", endpoint, len(data))
            except Exception as e:
                logger.exception("firefly %s export failed", endpoint)
                counts[endpoint] = f"error: {e}"
    return counts


def _dump_finance_yaml(work: Path) -> list[str]:
    """Copy every *.yaml in /finance to work/finance/."""
    out_dir = work / "finance"
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    if not FINANCE_DIR.exists():
        return files
    for f in sorted(FINANCE_DIR.glob("*.yaml")):
        shutil.copy(f, out_dir / f.name)
        files.append(f.name)
    return files


def _read_version() -> str:
    try:
        return Path("/app/VERSION").read_text().strip()
    except Exception:
        return "unknown"


async def run_backup() -> dict:
    """Single backup invocation. Returns the manifest dict."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    archive_path = BACKUP_DIR / f"sentinel-finance-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="sf-backup-") as tmpdir:
        work = Path(tmpdir)
        yaml_files = _dump_finance_yaml(work)
        ff_counts = await _dump_firefly(work)
        manifest = {
            "generated_at_utc": datetime.utcnow().isoformat() + "Z",
            "portfolio_mcp_version": _read_version(),
            "yaml_files": yaml_files,
            "firefly_counts": ff_counts,
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # Bundle into a single tar.gz
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(work, arcname=f"sentinel-finance-{stamp}")

    size_kb = archive_path.stat().st_size // 1024
    manifest["archive_path"] = str(archive_path)
    manifest["size_kb"] = size_kb
    logger.info("backup written: %s (%d KB)", archive_path, size_kb)

    pruned = _prune_old()
    manifest["pruned"] = pruned
    return manifest


def _prune_old() -> list[str]:
    """Delete archives older than RETENTION_DAYS. Returns paths pruned."""
    if not BACKUP_DIR.exists():
        return []
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    pruned: list[str] = []
    for f in BACKUP_DIR.glob("sentinel-finance-*.tar.gz"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            try:
                f.unlink()
                pruned.append(f.name)
            except Exception:
                logger.exception("failed to prune %s", f)
    return pruned


def list_backups() -> list[dict]:
    """Return existing backup files newest-first."""
    if not BACKUP_DIR.exists():
        return []
    out = []
    for f in sorted(BACKUP_DIR.glob("sentinel-finance-*.tar.gz"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        out.append({
            "name": f.name,
            "size_kb": f.stat().st_size // 1024,
            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    return out
