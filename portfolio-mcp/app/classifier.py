"""Counterparty / accounts classifier.

Single source of truth for "raw bank-statement description" -> canonical
vendor name + category + account type. Reusable across every CSV/PDF
importer (POSB, Maybank, SC, credit-card PDFs).

Loads from /finance/classifier.yaml on first import; cached in process.
Reload with reload_classifier() after editing the YAML.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

logger = logging.getLogger(__name__)

CLASSIFIER_PATH = Path("/finance/classifier.yaml")


@dataclass(frozen=True)
class ClassifierMatch:
    canonical: str
    category: str
    account_type: str  # expense | income | transfer | liability | investment
    expected_payment_account: str | None = None
    match_pattern: str = ""  # which pattern matched (debug aid)


_VENDORS: list[dict] | None = None


def _load() -> list[dict]:
    global _VENDORS
    if _VENDORS is not None:
        return _VENDORS
    try:
        data = yaml.safe_load(CLASSIFIER_PATH.read_text()) or {}
        _VENDORS = data.get("vendors", [])
    except FileNotFoundError:
        logger.warning("classifier.yaml not found at %s", CLASSIFIER_PATH)
        _VENDORS = []
    except Exception:
        logger.exception("classifier.yaml load failed")
        _VENDORS = []
    return _VENDORS


def reload_classifier() -> int:
    """Force re-read of classifier.yaml. Returns vendor count."""
    global _VENDORS
    _VENDORS = None
    return len(_load())


def lookup(description: str) -> ClassifierMatch | None:
    """First-substring-match wins. Case-insensitive."""
    if not description:
        return None
    desc_l = description.lower()
    for v in _load():
        for pattern in v.get("match", []):
            if pattern.lower() in desc_l:
                return ClassifierMatch(
                    canonical=v["canonical"],
                    category=v.get("category", "Uncategorised"),
                    account_type=v.get("account_type", "expense"),
                    expected_payment_account=v.get("expected_payment_account"),
                    match_pattern=pattern,
                )
    return None


DEFAULT_CATEGORY = "General Expense"


def classify_or_default(description: str, default_category: str = DEFAULT_CATEGORY) -> ClassifierMatch:
    """Always returns a match — falls back to a 'General Expense' bucket so
    every transaction has a category. Use /admin/classifier/edit to add
    specific rules and reclassify later.
    """
    m = lookup(description)
    if m:
        return m
    return ClassifierMatch(
        canonical=description[:50].strip() or "Unknown",
        category=default_category,
        account_type="expense",
        expected_payment_account=None,
        match_pattern="(no match)",
    )


def add_rule(canonical: str, match_pattern: str, category: str,
             account_type: str = "expense",
             expected_payment_account: str | None = None) -> dict:
    """Append a new rule to classifier.yaml and reload. Idempotent: if
    the same (canonical, pattern) already exists, no-op."""
    canonical = canonical.strip()
    match_pattern = match_pattern.strip()
    category = category.strip() or DEFAULT_CATEGORY
    account_type = account_type.strip() or "expense"
    if not canonical or not match_pattern:
        return {"ok": False, "error": "canonical + match_pattern required"}

    try:
        data = yaml.safe_load(CLASSIFIER_PATH.read_text()) or {}
    except FileNotFoundError:
        data = {"vendors": []}
    vendors = data.get("vendors") or []

    # If canonical exists, append the pattern to its match[] list
    for v in vendors:
        if v.get("canonical") == canonical:
            existing = [m.lower() for m in v.get("match", [])]
            if match_pattern.lower() not in existing:
                v.setdefault("match", []).append(match_pattern)
                _write_yaml(data)
                reload_classifier()
                return {"ok": True, "action": "appended_pattern",
                        "canonical": canonical, "pattern": match_pattern,
                        "total_rules": len(vendors)}
            return {"ok": True, "action": "noop_duplicate",
                    "canonical": canonical, "pattern": match_pattern,
                    "total_rules": len(vendors)}

    # New canonical — append full entry
    entry = {
        "canonical": canonical,
        "match": [match_pattern],
        "category": category,
        "account_type": account_type,
    }
    if expected_payment_account:
        entry["expected_payment_account"] = expected_payment_account
    vendors.append(entry)
    data["vendors"] = vendors
    _write_yaml(data)
    reload_classifier()
    return {"ok": True, "action": "added", "canonical": canonical,
            "pattern": match_pattern, "category": category,
            "account_type": account_type, "total_rules": len(vendors)}


def _write_yaml(data: dict):
    """Serialize back to /finance/classifier.yaml preserving readable shape."""
    CLASSIFIER_PATH.write_text(yaml.safe_dump(data, sort_keys=False,
                                               default_flow_style=False,
                                               allow_unicode=True))


def known_categories() -> list[str]:
    """Distinct categories currently in classifier.yaml — feeds dropdowns."""
    cats: set[str] = set()
    for v in _load():
        c = v.get("category")
        if c:
            cats.add(c)
    cats.add(DEFAULT_CATEGORY)
    return sorted(cats)


def known_account_types() -> list[str]:
    return ["expense", "income", "transfer", "liability", "investment"]


def unmatched_examples(descriptions: Iterable[str], limit: int = 30) -> list[dict]:
    """Group descriptions that don't match any classifier rule. Returns
    [{description, count}] sorted by count desc — feeds the /admin/classifier
    triage page."""
    counts: dict[str, int] = {}
    for d in descriptions:
        if d and not lookup(d):
            counts[d[:80]] = counts.get(d[:80], 0) + 1
    return [
        {"description": d, "count": c}
        for d, c in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    ]


def vendor_count() -> int:
    return len(_load())


def stats() -> dict:
    vendors = _load()
    by_type: dict[str, int] = {}
    for v in vendors:
        at = v.get("account_type", "expense")
        by_type[at] = by_type.get(at, 0) + 1
    return {
        "vendor_count": len(vendors),
        "by_account_type": by_type,
        "yaml_path": str(CLASSIFIER_PATH),
        "yaml_exists": CLASSIFIER_PATH.exists(),
    }
