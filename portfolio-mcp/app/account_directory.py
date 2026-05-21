"""Account directory — maps real-world account numbers to Firefly accounts.

Sources:
  • finance/liabilities-registry.yaml — CC + loan accounts (already maintained)
  • finance/asset_accounts.yaml      — bank/savings/wise (new, owner-edited)

Used by:
  • Pending Reconciliation page — shows matched account + suggests category
  • Importers — auto-classify when desc contains a known account number
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

LIAB_PATH = Path("/finance/liabilities-registry.yaml")
ASSET_PATH = Path("/finance/asset_accounts.yaml")

# Default mapping of liability `type` → classifier category
_LIAB_CATEGORY = {
    "credit_card": "Debt service",
    "term_loan":   "Debt service",
    "revolving":   "Debt service",
}


@dataclass(frozen=True)
class AccountEntry:
    firefly_account_id: int
    name: str
    account_numbers: tuple[str, ...]  # raw form (with formatting)
    kind: str                          # "asset" | "liability"
    category: str | None = None        # classifier category to suggest
    account_type: str = "expense"      # classifier account_type
    notes: str = ""


def _normalize(s: str) -> str:
    """Lower-case + strip non-alphanumerics. Used for substring matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _load() -> list[AccountEntry]:
    entries: list[AccountEntry] = []

    # 1. Liability accounts from liabilities-registry.yaml
    try:
        liab = yaml.safe_load(LIAB_PATH.read_text()) or {}
        for a in liab.get("accounts", []):
            num = (a.get("account_number") or "").strip()
            if not num:
                continue
            numbers = [num]
            # Also try the no-dashes form so we catch both formats
            no_dash = re.sub(r"[-\s]", "", num)
            if no_dash != num:
                numbers.append(no_dash)
            entries.append(AccountEntry(
                firefly_account_id=int(a["firefly_acct_id"]),
                name=a.get("name", a.get("id", "?")),
                account_numbers=tuple(numbers),
                kind="liability",
                category=_LIAB_CATEGORY.get(a.get("type"), "Debt service"),
                account_type="liability",
                notes=a.get("notes", ""),
            ))
    except FileNotFoundError:
        logger.info("liabilities-registry.yaml not present")
    except Exception:
        logger.exception("liabilities-registry.yaml parse failed")

    # 2. Asset accounts (bank, wise, savings) from asset_accounts.yaml
    try:
        asset = yaml.safe_load(ASSET_PATH.read_text()) or {}
        for a in asset.get("accounts", []):
            nums = a.get("account_numbers") or ([a["account_number"]]
                                                  if a.get("account_number") else [])
            if not nums:
                continue
            expanded: list[str] = []
            for n in nums:
                expanded.append(n)
                nd = re.sub(r"[-\s]", "", n)
                if nd != n:
                    expanded.append(nd)
            entries.append(AccountEntry(
                firefly_account_id=int(a["firefly_acct_id"]),
                name=a.get("name", "?"),
                account_numbers=tuple(expanded),
                kind="asset",
                category=a.get("category"),
                account_type=a.get("account_type", "expense"),
                notes=a.get("notes", ""),
            ))
    except FileNotFoundError:
        # Optional file — many users may not need this
        pass
    except Exception:
        logger.exception("asset_accounts.yaml parse failed")

    return entries


_CACHE: list[AccountEntry] | None = None


def all_entries() -> list[AccountEntry]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE


def reload_directory() -> int:
    global _CACHE
    _CACHE = None
    return len(all_entries())


def lookup_by_description(description: str) -> AccountEntry | None:
    """Return the AccountEntry whose any registered number appears in the
    description (substring, after normalising both to alphanumerics).
    Longest-number match wins so partial collisions don't false-match.
    """
    if not description:
        return None
    norm_desc = _normalize(description)
    best: tuple[int, AccountEntry] | None = None
    for entry in all_entries():
        for num in entry.account_numbers:
            n = _normalize(num)
            if len(n) < 5:  # too short to be reliable
                continue
            if n in norm_desc:
                score = len(n)
                if best is None or score > best[0]:
                    best = (score, entry)
                break
    return best[1] if best else None


def add_asset_account(firefly_account_id: int, name: str,
                      account_numbers: list[str],
                      category: str | None = None,
                      account_type: str = "expense",
                      notes: str = "") -> dict:
    """Append a new asset-account entry to asset_accounts.yaml + reload."""
    try:
        data = yaml.safe_load(ASSET_PATH.read_text()) or {}
    except FileNotFoundError:
        data = {"accounts": []}
    accts = data.get("accounts", []) or []
    # Idempotent: skip if firefly_account_id already present
    for a in accts:
        if int(a.get("firefly_acct_id") or 0) == firefly_account_id:
            # Append any new numbers
            existing = set(a.get("account_numbers") or
                           ([a.get("account_number")] if a.get("account_number") else []))
            added = [n for n in account_numbers if n not in existing]
            if not added:
                return {"ok": True, "action": "noop_exists",
                        "firefly_account_id": firefly_account_id}
            a["account_numbers"] = sorted(existing.union(added))
            ASSET_PATH.write_text(yaml.safe_dump(data, sort_keys=False,
                                                  default_flow_style=False))
            reload_directory()
            return {"ok": True, "action": "appended_numbers",
                    "firefly_account_id": firefly_account_id,
                    "added": added}
    accts.append({
        "firefly_acct_id": firefly_account_id,
        "name": name,
        "account_numbers": account_numbers,
        "category": category,
        "account_type": account_type,
        "notes": notes,
    })
    data["accounts"] = accts
    ASSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    ASSET_PATH.write_text(yaml.safe_dump(data, sort_keys=False,
                                          default_flow_style=False))
    reload_directory()
    return {"ok": True, "action": "added",
            "firefly_account_id": firefly_account_id, "name": name,
            "numbers": account_numbers}


def stats() -> dict:
    entries = all_entries()
    by_kind: dict[str, int] = {}
    for e in entries:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    return {
        "total": len(entries),
        "by_kind": by_kind,
        "liab_path": str(LIAB_PATH),
        "asset_path": str(ASSET_PATH),
        "asset_path_exists": ASSET_PATH.exists(),
    }
