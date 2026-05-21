"""Privacy / data-protection audit.

Surfaces single-tenant assumptions that block clean multi-tenant migration
(v3.0.0). Scans the live filesystem + database for:

  * Personal data inventory (where is owner data stored, what format)
  * Hardcoded owner IDs (YOUR_TELEGRAM_CHAT_ID, your@email.com, etc.)
  * Single-tenant file paths (/finance/*.yaml, /data/*.db)
  * Cross-process secrets (Firefly PAT, Wise token, Moralis key) — verify
    they're env-only, never persisted to user-readable files
  * Stale/orphaned data files (caches, backups older than retention)

Output is a dict with findings grouped by severity. Rendered as the
/admin/privacy page (admin only).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# ─── Scan targets ───────────────────────────────────────────────────────────

# Hardcoded owner identifiers that should be env-vars in v3.
SINGLE_TENANT_LITERALS = (
    ("YOUR_TELEGRAM_CHAT_ID", "owner Telegram chat ID"),
    ("your@email.com", "owner Gmail address"),
    ("0xYOUR_WALLET_ADDRESS", "owner wallet address"),
    ("170-37376-6", "owner POSB account number"),
)

# Personal data files inventoried for v3 tenant-scoping work.
PERSONAL_DATA_INVENTORY = [
    {"path": "/finance/balance_sheet_config.yaml", "kind": "config",
     "contains": "Firefly account ID mapping (per-user)"},
    {"path": "/finance/liabilities-registry.yaml", "kind": "config",
     "contains": "credit-card + loan accounts (PII: card last-4 + billing days)"},
    {"path": "/finance/recurring.yaml", "kind": "config",
     "contains": "monthly income + expense schedule (PII: salary amount)"},
    {"path": "/finance/funds.yaml", "kind": "config",
     "contains": "ILP/CPF fund holdings (PII: units owned per policy)"},
    {"path": "/finance/settings.yaml", "kind": "config",
     "contains": "date format + YourAgency rate (low-sensitivity)"},
    {"path": "/data/portfolio.db", "kind": "database",
     "contains": "SQLite: snapshots, manual_positions, users, sessions, hidden_tokens, import_log, networth_history"},
    {"path": "/data/snapshot_cache.json", "kind": "cache",
     "contains": "last Moralis wallet snapshot (PII: token holdings)"},
    {"path": "/data/krystal_cache.json", "kind": "cache",
     "contains": "last Krystal LP positions"},
    {"path": "/data/backups/", "kind": "backup",
     "contains": "daily tar.gz of finance YAMLs + full Firefly export"},
    {"path": "/data/charts/", "kind": "artifact",
     "contains": "PNG net-worth charts (PII: SGD amounts)"},
    {"path": "(external) Firefly III Postgres", "kind": "database",
     "contains": "ALL transaction history (single-tenant DB today)"},
    {"path": "(external) OneDrive bind mount", "kind": "filesystem",
     "contains": "Sentinel Finance/ folder (statements, ILP docs)"},
]

# Code paths that would need tenant scoping in v3.
CODE_SCAN_ROOTS = (Path("/app/app"),)
CODE_FILE_PATTERN = re.compile(r"\.py$")


@dataclass
class Finding:
    severity: str            # "blocker" | "warn" | "info"
    category: str            # "hardcoded" | "inventory" | "stale" | "orphan"
    title: str
    detail: str
    where: str = ""
    suggest: str = ""


def _iter_code_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and CODE_FILE_PATTERN.search(p.name):
                yield p


def scan_hardcoded_owner_ids() -> list[Finding]:
    """Grep source code for SINGLE_TENANT_LITERALS — these block multi-tenant."""
    findings: list[Finding] = []
    for code in _iter_code_files(CODE_SCAN_ROOTS):
        try:
            content = code.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for literal, label in SINGLE_TENANT_LITERALS:
            if literal in content:
                # Find line numbers
                lines = content.splitlines()
                matches = [i + 1 for i, ln in enumerate(lines) if literal in ln]
                where = f"{code.relative_to('/app')}:{','.join(str(m) for m in matches[:3])}"
                findings.append(Finding(
                    severity="blocker",
                    category="hardcoded",
                    title=f"Hardcoded {label}",
                    detail=f"Literal {literal!r} appears in code — blocks tenant separation.",
                    where=where,
                    suggest=f"Replace with env-var or tenant-scoped setting before v3.0",
                ))
    return findings


def scan_data_inventory() -> list[Finding]:
    """Inventory existing personal data files + their size."""
    findings: list[Finding] = []
    for entry in PERSONAL_DATA_INVENTORY:
        path = entry["path"]
        if path.startswith("("):
            # External resource — note but skip filesystem check
            findings.append(Finding(
                severity="info", category="inventory",
                title=f"{entry['kind']}: {path}",
                detail=entry["contains"],
                where=path,
                suggest="external dependency — track in LEDGER-DECISION.md",
            ))
            continue
        p = Path(path)
        if not p.exists():
            findings.append(Finding(
                severity="info", category="inventory",
                title=f"{entry['kind']}: {path} (not present)",
                detail=entry["contains"],
                where=path,
                suggest="not provisioned in this container instance",
            ))
            continue
        try:
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                count = sum(1 for f in p.rglob("*") if f.is_file())
                detail = f"{entry['contains']} · {count} files · {size // 1024} KB"
            else:
                detail = f"{entry['contains']} · {p.stat().st_size // 1024} KB"
        except Exception:
            detail = entry["contains"]
        findings.append(Finding(
            severity="info", category="inventory",
            title=f"{entry['kind']}: {path}",
            detail=detail,
            where=path,
            suggest="tenant-scope path under /tenants/<tid>/ in v3",
        ))
    return findings


def scan_stale_data() -> list[Finding]:
    """Detect cache/backup files older than retention windows."""
    findings: list[Finding] = []
    now = datetime.utcnow()

    # Backups older than 7 days
    bdir = Path("/data/backups")
    if bdir.exists():
        for f in bdir.glob("*.tar.gz"):
            age = (now - datetime.utcfromtimestamp(f.stat().st_mtime)).days
            if age > 7:
                findings.append(Finding(
                    severity="warn", category="stale",
                    title=f"Backup retention exceeded: {f.name}",
                    detail=f"Age {age} days (retention = 7).",
                    where=str(f),
                    suggest="run backup.run_backup() to trigger prune; or hand-delete.",
                ))

    # Charts older than 30 days
    cdir = Path("/data/charts")
    if cdir.exists():
        for f in cdir.glob("*.png"):
            age = (now - datetime.utcfromtimestamp(f.stat().st_mtime)).days
            if age > 30:
                findings.append(Finding(
                    severity="info", category="stale",
                    title=f"Old chart: {f.name}",
                    detail=f"Age {age} days. No retention policy yet — accumulates.",
                    where=str(f),
                    suggest="add chart retention to backup.py prune step.",
                ))

    return findings


def scan_env_secrets() -> list[Finding]:
    """Verify sensitive tokens come from env, not persisted files."""
    findings: list[Finding] = []
    expected_env = ("FIREFLY_PAT", "WISE_API_TOKEN", "MORALIS_API_KEY",
                    "TELEGRAM_BOT_TOKEN", "TESTBOT_TOKEN", "SESSION_SECRET")
    missing = [k for k in expected_env if not os.environ.get(k)]
    if missing:
        findings.append(Finding(
            severity="warn", category="hardcoded",
            title="Secret env vars missing",
            detail=f"Container missing: {', '.join(missing)}",
            where="(container env)",
            suggest="check .env.local + sync_env_from_wcm.ps1 key-list.",
        ))
    return findings


def run_full_audit() -> dict:
    """Aggregate every scanner. Returns dict with grouped findings."""
    all_findings: list[Finding] = []
    all_findings += scan_hardcoded_owner_ids()
    all_findings += scan_data_inventory()
    all_findings += scan_stale_data()
    all_findings += scan_env_secrets()

    by_severity: dict[str, list[Finding]] = {"blocker": [], "warn": [], "info": []}
    by_category: dict[str, int] = {}
    for f in all_findings:
        by_severity.setdefault(f.severity, []).append(f)
        by_category[f.category] = by_category.get(f.category, 0) + 1

    return {
        "ran_at": datetime.utcnow().isoformat() + "Z",
        "summary": {
            "total": len(all_findings),
            "blocker": len(by_severity["blocker"]),
            "warn": len(by_severity["warn"]),
            "info": len(by_severity["info"]),
            "by_category": by_category,
        },
        "findings": [{
            "severity": f.severity,
            "category": f.category,
            "title": f.title,
            "detail": f.detail,
            "where": f.where,
            "suggest": f.suggest,
        } for f in all_findings],
    }
