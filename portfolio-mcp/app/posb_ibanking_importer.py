"""POSB iBanking transaction-history CSV → Firefly III.

The CSV the user downloads from POSB iBanking ("Recent Transactions" export)
looks like:

    "Account Details For:","POSBkids Account 170-37376-6"
    "Statement as at:","13 May 2026"
    ""
    "Available Balance:","SGD 654.84"
    "Ledger Balance:","SGD 733.12"
    ""
    "Transaction Date","Transaction Code","Description","Transaction Ref1",
    "Transaction Ref2","Transaction Ref3","Status","Debit Amount",
    "Credit Amount"
    "13 May 2026","ICT","Wise:...","...","Transfer","OTHR 17...","Settled",20,""
    ...

This is DIFFERENT from the monthly statement PDF format handled by
scripts/posb_to_firefly_csv.py. The watcher in this module imports these
on-demand or scheduled.

The matcher uses Firefly's `error_if_duplicate_hash` so re-imports of the
same CSV are safe.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import shutil
from datetime import datetime, date
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
DROP_ROOT = Path("/onedrive/Sentinel Finance/Auto-import")
PROCESSED_ROOT = DROP_ROOT / "_processed"

# POSB account-number → Firefly asset account id (numeric).
# Single source of truth; extend as more accounts are imported.
POSB_ACCOUNT_MAP = {
    "170-37376-6": 1,   # POSB Savings (Firefly id=1)
}


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

DATE_RE = re.compile(r"^\d{1,2}\s+\w{3}\s+\d{4}$")


def _parse_date(s: str) -> str:
    """'13 May 2026' → '2026-05-13' (ISO)."""
    return datetime.strptime(s, "%d %b %Y").strftime("%Y-%m-%d")


def _to_amount(s: str) -> float:
    if not s or s.strip() == "":
        return 0.0
    return float(str(s).replace(",", "").strip())


def parse_csv(path: Path) -> dict:
    """Parse a POSB iBanking CSV. Returns:
      {
        "account_number": str | None,
        "statement_date": str | None,
        "available_balance": float | None,
        "ledger_balance": float | None,
        "transactions": [{date, description, debit, credit, amount, ref, code}],
      }
    """
    out = {
        "account_number": None,
        "statement_date": None,
        "available_balance": None,
        "ledger_balance": None,
        "transactions": [],
    }
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    header_idx = None
    for i, row in enumerate(rows):
        if not row:
            continue
        cell0 = row[0].strip() if row else ""
        # Header lines
        if cell0.startswith("Account Details For:") and len(row) > 1:
            m = re.search(r"(\d{3}-\d{5}-\d)", row[1])
            if m:
                out["account_number"] = m.group(1)
        elif cell0.startswith("Statement as at:") and len(row) > 1:
            try:
                out["statement_date"] = _parse_date(row[1].strip())
            except ValueError:
                pass
        elif cell0.startswith("Available Balance:") and len(row) > 1:
            out["available_balance"] = _to_amount(row[1].replace("SGD", "").strip())
        elif cell0.startswith("Ledger Balance:") and len(row) > 1:
            out["ledger_balance"] = _to_amount(row[1].replace("SGD", "").strip())
        elif cell0 == "Transaction Date":
            header_idx = i
            break

    if header_idx is None:
        return out

    for row in rows[header_idx + 1:]:
        if not row or len(row) < 9:
            continue
        tdate_s = row[0].strip()
        if not DATE_RE.match(tdate_s):
            continue
        debit = _to_amount(row[7])
        credit = _to_amount(row[8])
        amount = credit - debit  # +inflow, -outflow
        if amount == 0:
            continue
        out["transactions"].append({
            "date": _parse_date(tdate_s),
            "code": row[1].strip(),
            "description": row[2].strip(),
            "ref1": row[3].strip(),
            "ref2": row[4].strip(),
            "ref3": row[5].strip(),
            "debit": debit,
            "credit": credit,
            "amount": round(amount, 2),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Counterparty extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_counterparty(tx: dict) -> str:
    """Best-effort canonical counterparty for a POSB iBanking row.

    Delegates to app.classifier (single source of truth in classifier.yaml).
    """
    from . import classifier
    desc = tx["description"]
    m = classifier.lookup(desc)
    if m:
        return m.canonical
    # PayNow "To: X" pattern — extract beneficiary as fallback
    pn = re.search(r"To:\s*([^O]+?)(?:\s+OTHR|\s+\d|$)", desc)
    if pn:
        return pn.group(1).strip()
    return desc[:50].strip() or "Unknown"


def classify_tx(tx: dict):
    """Full ClassifierMatch lookup — exposes category + account_type, not
    just the canonical name. Used by the iBanking importer to set Firefly
    category_name + pick the right transaction type.

    Falls back to classifier.classify_or_default (General Expense bucket)
    so every transaction gets a category — never blank.
    """
    from . import classifier
    return classifier.classify_or_default(tx["description"])


# ─────────────────────────────────────────────────────────────────────────────
# Firefly import
# ─────────────────────────────────────────────────────────────────────────────

def _firefly_headers() -> dict:
    pat = os.environ.get("FIREFLY_PAT", "")
    return {"Authorization": f"Bearer {pat}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _account_name(account_id: int) -> str:
    """Lookup Firefly account name by id (sync httpx)."""
    try:
        r = httpx.get(f"{FIREFLY_URL}/api/v1/accounts/{account_id}",
                      headers=_firefly_headers(), timeout=8)
        return r.json()["data"]["attributes"]["name"]
    except Exception:
        return f"account#{account_id}"


def _post_transaction(tx: dict, account_id: int, account_name: str) -> tuple[str, str]:
    """Returns (status, detail). status ∈ {created, dup, error}."""
    amount = tx["amount"]
    is_outflow = amount < 0
    match = classify_tx(tx)
    counterparty = match.canonical
    notes_parts = [tx["description"]]
    for r in (tx["ref1"], tx["ref2"], tx["ref3"]):
        if r and r not in notes_parts:
            notes_parts.append(r)
    txn = {
        "date": tx["date"],
        "amount": f"{abs(amount):.2f}",
        "description": (tx["description"] or counterparty)[:255],
        "notes": (" | ".join(notes_parts))[:1000],
        "external_id": (tx["ref3"] or tx["ref1"] or tx["description"])[:255],
        # Apply category from classifier
        "category_name": match.category[:255],
    }
    if is_outflow:
        txn["type"] = "withdrawal"
        txn["source_name"] = account_name
        txn["destination_name"] = counterparty[:255] or "Unknown"
    else:
        txn["type"] = "deposit"
        txn["source_name"] = counterparty[:255] or "Unknown"
        txn["destination_name"] = account_name
    payload = {
        "error_if_duplicate_hash": True,
        "apply_rules": True,
        "fire_webhooks": False,
        "transactions": [txn],
    }
    try:
        r = httpx.post(f"{FIREFLY_URL}/api/v1/transactions",
                       headers=_firefly_headers(), json=payload, timeout=15)
        if r.status_code in (200, 201):
            return ("created", f"posted {tx['date']} {amount:+.2f}")
        body = r.text[:300]
        if "Duplicate" in body or "duplicate" in body:
            return ("dup", "duplicate hash")
        return ("error", f"HTTP {r.status_code} · {body[:120]}")
    except Exception as e:
        return ("error", str(e)[:200])


def import_file(path: Path) -> dict:
    """Parse a POSB iBanking CSV and POST each row to Firefly.

    Returns: {file, account_number, account_id, created, dup, errored, errors}.
    """
    parsed = parse_csv(path)
    acct_num = parsed.get("account_number")
    acct_id = POSB_ACCOUNT_MAP.get(acct_num or "")
    if not acct_id:
        return {"file": str(path), "account_number": acct_num,
                "account_id": None, "created": 0, "dup": 0, "errored": 0,
                "errors": [f"unmapped account {acct_num}"]}
    acct_name = _account_name(acct_id)
    counts = {"created": 0, "dup": 0, "error": 0}
    errors: list[str] = []
    for tx in parsed["transactions"]:
        status, detail = _post_transaction(tx, acct_id, acct_name)
        counts[status] = counts.get(status, 0) + 1
        if status == "error":
            errors.append(f"{tx['date']} {tx['amount']:+.2f}: {detail}")
    return {
        "file": path.name,
        "account_number": acct_num,
        "account_id": acct_id,
        "account_name": acct_name,
        "available_balance": parsed.get("available_balance"),
        "ledger_balance": parsed.get("ledger_balance"),
        "n_rows": len(parsed["transactions"]),
        "created": counts["created"],
        "dup": counts["dup"],
        "errored": counts["error"],
        "errors": errors[:10],
    }


def move_to_processed(src: Path) -> Path:
    """Move imported CSV to _processed/YYYY-MM-DD-<original-name>."""
    PROCESSED_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    dst = PROCESSED_ROOT / f"{stamp}-{src.name}"
    # If dst exists already, append a counter
    counter = 1
    while dst.exists():
        dst = PROCESSED_ROOT / f"{stamp}-{counter}-{src.name}"
        counter += 1
    shutil.move(str(src), str(dst))
    return dst


# ─────────────────────────────────────────────────────────────────────────────
# Watcher entry point
# ─────────────────────────────────────────────────────────────────────────────

def _current_firefly_balance(account_id: int) -> float | None:
    """Pull current_balance from Firefly for post-import reconciliation."""
    try:
        r = httpx.get(f"{FIREFLY_URL}/api/v1/accounts/{account_id}",
                      headers=_firefly_headers(), timeout=8)
        if r.status_code == 200:
            return float(r.json()["data"]["attributes"]["current_balance"])
    except Exception:
        pass
    return None


def _log_import(result: dict, triggered_by: str):
    """Persist an ImportLog row for /config/imports history."""
    try:
        from . import database as db
        s = db.SessionLocal()
        try:
            row = db.ImportLog(
                started_at=datetime.utcnow(),
                source="posb_ibanking",
                file_name=result.get("file", ""),
                account_id=result.get("account_id"),
                account_name=result.get("account_name"),
                n_rows=result.get("n_rows", 0),
                created=result.get("created", 0),
                duplicates=result.get("dup", 0),
                errored=result.get("errored", 0),
                ledger_balance=result.get("ledger_balance"),
                firefly_balance=result.get("firefly_balance"),
                variance=result.get("variance"),
                error_summary=" | ".join(result.get("errors", []))[:500] if result.get("errors") else None,
                moved_to=result.get("moved_to"),
                triggered_by=triggered_by,
            )
            s.add(row)
            s.commit()
        finally:
            s.close()
    except Exception:
        logger.exception("ImportLog write failed for %s", result.get("file"))


def scan_and_import(move_after: bool = True, triggered_by: str = "manual") -> dict:
    """Walk every Auto-import/<bank>/ folder and import any *.csv it finds.

    Skips files that are inside the _processed/ tree.
    Post-import: reads Firefly current_balance and records variance against
    the statement ledger from the CSV (Task #20 auto-reconcile).
    Each result is persisted into the ImportLog table (Task #28).

    Returns: {scanned, results: [per-file dicts]}.
    """
    from datetime import datetime  # noqa: F401 (used by _log_import)
    results = []
    if not DROP_ROOT.exists():
        return {"scanned": 0, "results": [],
                "error": f"{DROP_ROOT} not present in container"}
    for csv_path in sorted(DROP_ROOT.glob("**/*.csv")):
        if PROCESSED_ROOT in csv_path.parents:
            continue
        try:
            r = import_file(csv_path)
            # Post-import reconciliation
            if r.get("account_id"):
                ff_bal = _current_firefly_balance(r["account_id"])
                if ff_bal is not None:
                    r["firefly_balance"] = ff_bal
                    if r.get("ledger_balance") is not None:
                        r["variance"] = round(ff_bal - r["ledger_balance"], 2)
            if move_after and r.get("errored", 0) == 0 and r.get("n_rows", 0) > 0:
                moved_to = move_to_processed(csv_path)
                r["moved_to"] = str(moved_to)
            _log_import(r, triggered_by)
            results.append(r)
        except Exception as e:
            logger.exception("import_file failed for %s", csv_path)
            err = {"file": csv_path.name, "error": str(e)[:200]}
            _log_import(err, triggered_by)
            results.append(err)
    return {"scanned": len(results), "results": results}
