"""
cpf_pdf_to_firefly.py — Parse CPF Transaction History PDF, create the 3 CPF
asset accounts (OA, SA, MA) with opening balances from the BAL line, and
post each transaction row to Firefly III via REST API.

Idempotent on re-run: existing accounts are updated (PUT), transactions use
`error_if_duplicate_hash: True` so re-runs are safe.

Codes handled:
  BAL  — opening/closing balance (skipped during transaction import)
  CON  — contribution from employer (revenue source by Ref)
  INT  — interest (revenue source "CPF Interest")
  INV  — flow into CPF Investment Scheme (transfer to CPF-IS asset acct)
  DPS  — Dependants' Protection Scheme deduction (expense)
  MSL  — MediShield Life deduction (expense)
  PMI  — Integrated Shield Plan deduction (expense)
  CSL  — CareShield Life deduction (expense)
  SUP  — ElderShield Supplement deduction (expense)
  Other codes — generic deposit/withdrawal with the code in description

Usage:
  py scripts/cpf_pdf_to_firefly.py --dry-run
  py scripts/cpf_pdf_to_firefly.py
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path

import pdfplumber

PDF_PATH = Path(r"C:\Users\azfar\OneDrive\CPF Statements\Transaction history.pdf")
FIREFLY_BASE = "http://127.0.0.1:8180"
PAT_FILE = Path(os.path.expandvars(r"%TEMP%\firefly_pat.txt"))

# Row regex: DATE CODE [REF] OA SA MA
ROW_RE = re.compile(
    r'^(\d{1,2}\s+\w+\s+\d{4})\s+([A-Z]{3})\s+(.*?)\s*(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})\s*$'
)

EMPLOYER_MAP = {
    "A": "AZ United Pte Ltd",
    "B": "YourAgency Security Services",
    "C": "EDUSAVE/PSEA Transfer",
}

CODE_TO_EXPENSE = {
    "DPS": "DPS Insurance (Dependants' Protection Scheme)",
    "MSL": "MediShield Life",
    "PMI": "Integrated Shield Plan",
    "CSL": "CareShield Life",
    "SUP": "ElderShield Supplement",
}


def pat() -> str:
    return PAT_FILE.read_text(encoding="utf-8-sig").strip()


def call_ff(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    headers = {
        "Authorization": f"Bearer {pat()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{FIREFLY_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def find_existing_account(name: str) -> int | None:
    code, body = call_ff("GET", f"/api/v1/search/accounts?query={name.replace(' ', '%20')}&type=asset&field=name")
    if code != 200:
        return None
    for a in body.get("data", []):
        if a["attributes"]["name"] == name:
            return int(a["id"])
    return None


def upsert_asset_account(name: str, opening_balance: float, opening_date: str, notes: str) -> int:
    existing = find_existing_account(name)
    payload = {
        "name": name,
        "type": "asset",
        "account_role": "savingAsset",
        "currency_code": "SGD",
        "opening_balance": f"{opening_balance:.2f}",
        "opening_balance_date": opening_date,
        "notes": notes,
    }
    if existing:
        code, body = call_ff("PUT", f"/api/v1/accounts/{existing}", payload)
        action = "UPDATED"
        aid = existing
    else:
        code, body = call_ff("POST", "/api/v1/accounts", payload)
        if code >= 400:
            print(f"  account POST failed for {name}: {body}")
            sys.exit(1)
        aid = int(body["data"]["id"])
        action = "CREATED"
    print(f"  {action}  id={aid:>3}  {name:<30}  opening SGD {opening_balance:,.2f}")
    return aid


def parse_rows(pdf_path: Path) -> list[dict]:
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                clean = re.sub(r"\s+", " ", line).strip()
                m = ROW_RE.match(clean)
                if not m:
                    continue
                ds, code, ref, oa, sa, ma = m.groups()
                rows.append({
                    "date": datetime.strptime(ds, "%d %b %Y").date(),
                    "code": code,
                    "ref": ref.strip(),
                    "oa": float(oa.replace(",", "")),
                    "sa": float(sa.replace(",", "")),
                    "ma": float(ma.replace(",", "")),
                })
    return rows


def transactions_for_row(row: dict, acct_ids: dict, cpf_is_id: int) -> list[dict]:
    """Generate one or more Firefly transactions for this CPF history row."""
    if row["code"] == "BAL":
        return []  # balances are not transactions

    out = []
    for sub_acct, amt in (("OA", row["oa"]), ("SA", row["sa"]), ("MA", row["ma"])):
        if amt == 0:
            continue
        acct_id = acct_ids[sub_acct]
        code = row["code"]
        ref = row["ref"]
        date_str = row["date"].isoformat()
        abs_amt = abs(amt)
        ref_tag = f"[CPF {code}{(' '+ref) if ref else ''}]"

        if amt > 0:
            # deposit into the CPF account
            if code == "CON":
                # Employer contribution — ref ends in A/B/C
                emp_key = ref.split()[-1] if ref else ""
                source = EMPLOYER_MAP.get(emp_key, "Unknown Employer")
                description = f"{source} contribution to {sub_acct}"
                tx = {
                    "type": "deposit",
                    "date": date_str,
                    "amount": f"{abs_amt:.2f}",
                    "currency_code": "SGD",
                    "description": description,
                    "source_name": source,
                    "destination_id": acct_id,
                    "category_name": "CPF contribution",
                    "tags": ["cpf", sub_acct.lower()],
                    "notes": ref_tag,
                }
            elif code == "INT":
                tx = {
                    "type": "deposit",
                    "date": date_str,
                    "amount": f"{abs_amt:.2f}",
                    "currency_code": "SGD",
                    "description": f"CPF interest credited to {sub_acct}",
                    "source_name": "CPF Interest",
                    "destination_id": acct_id,
                    "category_name": "CPF interest",
                    "tags": ["cpf", "interest", sub_acct.lower()],
                    "notes": ref_tag,
                }
            elif code == "INV":
                # rare: INV with positive (e.g. proceeds back to OA)
                tx = {
                    "type": "transfer",
                    "date": date_str,
                    "amount": f"{abs_amt:.2f}",
                    "currency_code": "SGD",
                    "description": f"CPF-IS withdrawal back to {sub_acct}",
                    "source_id": cpf_is_id,
                    "destination_id": acct_id,
                    "category_name": "CPF Investment",
                    "tags": ["cpf", "cpf-is", sub_acct.lower()],
                    "notes": ref_tag,
                }
            else:
                tx = {
                    "type": "deposit",
                    "date": date_str,
                    "amount": f"{abs_amt:.2f}",
                    "currency_code": "SGD",
                    "description": f"CPF {code} to {sub_acct}",
                    "source_name": f"CPF {code}",
                    "destination_id": acct_id,
                    "tags": ["cpf", sub_acct.lower(), code.lower()],
                    "notes": ref_tag,
                }
        else:
            # withdrawal from the CPF account
            if code == "INV":
                tx = {
                    "type": "transfer",
                    "date": date_str,
                    "amount": f"{abs_amt:.2f}",
                    "currency_code": "SGD",
                    "description": f"{sub_acct} to CPF Investment Scheme",
                    "source_id": acct_id,
                    "destination_id": cpf_is_id,
                    "category_name": "CPF Investment",
                    "tags": ["cpf", "cpf-is", sub_acct.lower()],
                    "notes": ref_tag,
                }
            elif code in CODE_TO_EXPENSE:
                tx = {
                    "type": "withdrawal",
                    "date": date_str,
                    "amount": f"{abs_amt:.2f}",
                    "currency_code": "SGD",
                    "description": f"{CODE_TO_EXPENSE[code]} ({sub_acct})",
                    "source_id": acct_id,
                    "destination_name": CODE_TO_EXPENSE[code],
                    "category_name": "Insurance",
                    "tags": ["cpf", sub_acct.lower(), code.lower()],
                    "notes": ref_tag,
                }
            else:
                tx = {
                    "type": "withdrawal",
                    "date": date_str,
                    "amount": f"{abs_amt:.2f}",
                    "currency_code": "SGD",
                    "description": f"CPF {code} from {sub_acct}",
                    "source_id": acct_id,
                    "destination_name": f"CPF {code}",
                    "tags": ["cpf", sub_acct.lower(), code.lower()],
                    "notes": ref_tag,
                }
        out.append(tx)
    return out


def post_tx(tx: dict) -> tuple[str, str]:
    payload = {
        "error_if_duplicate_hash": True,
        "apply_rules": False,
        "fire_webhooks": False,
        "transactions": [tx],
    }
    code, body = call_ff("POST", "/api/v1/transactions", payload)
    if code == 200 or code == 201:
        return ("ok", str(body.get("data", {}).get("id", "?")))
    if isinstance(body, str) and ("duplicate" in body.lower() or "Duplicate" in body):
        return ("dup", body[:200])
    if isinstance(body, dict) and "duplicate" in json.dumps(body).lower():
        return ("dup", json.dumps(body)[:200])
    return ("err", json.dumps(body)[:400] if isinstance(body, dict) else str(body)[:400])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"Parsing {PDF_PATH.name}...")
    rows = parse_rows(PDF_PATH)
    print(f"  {len(rows)} rows extracted")

    # Find the BAL row at the start
    opening = next((r for r in rows if r["code"] == "BAL"), None)
    closing = [r for r in rows if r["code"] == "BAL"][-1] if any(r["code"] == "BAL" for r in rows) else None
    if not opening or not closing:
        print("  ERROR: missing BAL rows")
        sys.exit(1)
    print(f"  opening {opening['date']}: OA {opening['oa']:,.2f}  SA {opening['sa']:,.2f}  MA {opening['ma']:,.2f}")
    print(f"  closing {closing['date']}: OA {closing['oa']:,.2f}  SA {closing['sa']:,.2f}  MA {closing['ma']:,.2f}")

    open_date = opening["date"].isoformat()

    print("\n=== Upserting CPF accounts ===")
    if args.dry_run:
        print(f"  (dry-run) Would create CPF OA at {opening['oa']:,.2f}")
        print(f"  (dry-run) Would create CPF SA at {opening['sa']:,.2f}")
        print(f"  (dry-run) Would create CPF MA at {opening['ma']:,.2f}")
        print(f"  (dry-run) Would create CPF-IS at 0.00 (user will update)")
        acct_ids = {"OA": -1, "SA": -2, "MA": -3}
        cpf_is_id = -4
    else:
        acct_ids = {
            "OA": upsert_asset_account("CPF OA", opening["oa"], open_date,
                                       "Ordinary Account (2.5% p.a.). Auto-imported from CPF transaction history."),
            "SA": upsert_asset_account("CPF SA", opening["sa"], open_date,
                                       "Special Account (4% p.a.). Auto-imported."),
            "MA": upsert_asset_account("CPF MA", opening["ma"], open_date,
                                       "MediSave Account (4% p.a.). Auto-imported."),
        }
        cpf_is_id = upsert_asset_account(
            "CPF Investment Scheme", 0.0, open_date,
            "CPF-IS portfolio funded from OA. Update balance manually from CPF-IS dashboard."
        )

    print("\n=== Importing transactions ===")
    counts = {"ok": 0, "dup": 0, "err": 0, "bal": 0}
    sample_errors = []
    for r in rows:
        if r["code"] == "BAL":
            counts["bal"] += 1
            continue
        txs = transactions_for_row(r, acct_ids, cpf_is_id)
        for tx in txs:
            if args.dry_run:
                print(f"  WOULD POST  {tx['date']}  {tx['type']:10}  SGD {tx['amount']:>10}  {tx['description']}")
                counts["ok"] += 1
                continue
            status, info = post_tx(tx)
            counts[status] += 1
            if status == "err" and len(sample_errors) < 5:
                sample_errors.append((tx, info))

    print(f"\n  ok={counts['ok']}  dup={counts['dup']}  err={counts['err']}  bal_skipped={counts['bal']}")
    if sample_errors:
        print("\nSample errors:")
        for tx, err in sample_errors:
            print(f"  {tx['date']} {tx['description']}: {err[:250]}")


if __name__ == "__main__":
    main()
