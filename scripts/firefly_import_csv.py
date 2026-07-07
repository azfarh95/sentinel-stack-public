"""
Drive a Firefly III CSV import via the REST API.
Reads the PAT from %TEMP%\\firefly_pat.txt and POSTs each row to
/api/v1/transactions. Handles duplicates by skipping on 422 with
'duplicate' in the error body. Progress + final summary.
"""
import csv
import glob
import json
import os
import sys
import time
import urllib.request
import urllib.error

PAT_PATH = os.path.expandvars(r"%TEMP%\firefly_pat.txt")
BASE_URL = "http://127.0.0.1:8180"
ASSET_ACCOUNT_NAME = "POSB Savings"

def load_pat():
    with open(PAT_PATH, encoding="utf-8") as f:
        return f.read().strip()

def post_transaction(pat, row):
    """Build a Firefly transaction payload from a CSV row and POST it."""
    amount = float(row["amount"])
    is_withdrawal = amount < 0
    abs_amount = f"{abs(amount):.2f}"

    txn = {
        "date": row["date"],
        "amount": abs_amount,
        "description": row["description"] or "(no description)",
        "notes": row.get("notes", "")[:1000],
    }

    if is_withdrawal:
        txn["type"] = "withdrawal"
        txn["source_name"] = ASSET_ACCOUNT_NAME
        txn["destination_name"] = (row.get("destination_name") or "Unknown")[:255]
    else:
        txn["type"] = "deposit"
        txn["source_name"] = (row.get("source_name") or "Unknown")[:255]
        txn["destination_name"] = ASSET_ACCOUNT_NAME

    # Category + tags (optional)
    cat = row.get("category", "").strip()
    if cat:
        txn["category_name"] = cat
    tags = row.get("tags", "").strip()
    if tags:
        txn["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

    payload = {
        "error_if_duplicate_hash": True,
        "apply_rules": False,
        "fire_webhooks": False,
        "group_title": None,
        "transactions": [txn],
    }

    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/transactions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return ("ok", resp.status, None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        # Detect duplicate vs real error
        if "Duplicate" in body or "duplicate" in body:
            return ("dup", e.code, body)
        return ("err", e.code, body)
    except Exception as e:
        return ("err", 0, str(e)[:200])

def import_file(csv_path, pat):
    print(f"\n=== Importing {os.path.basename(csv_path)} ===")
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    total = len(rows)
    ok = dup = err = 0
    start = time.time()
    last_print = 0
    errors = []

    for i, row in enumerate(rows, 1):
        status, code, body = post_transaction(pat, row)
        if status == "ok": ok += 1
        elif status == "dup": dup += 1
        else:
            err += 1
            if len(errors) < 20:
                errors.append((row["date"], row["description"][:60], code, body[:200]))

        # Progress every 30s OR every 100 rows
        now = time.time()
        if (i % 100 == 0) or (now - last_print > 30):
            elapsed = now - start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(f"  [{i:4d}/{total}]  ok={ok}  dup={dup}  err={err}  rate={rate:.1f}/s  eta={eta:.0f}s")
            last_print = now

    elapsed = time.time() - start
    print(f"  DONE: {total} rows in {elapsed:.0f}s")
    print(f"  ok={ok}  duplicates={dup}  errors={err}")
    if errors:
        print(f"  First {len(errors)} error sample(s):")
        for d, desc, code, body in errors[:10]:
            print(f"    {d}  HTTP {code}  {desc!r}  body={body[:120]}")
    return ok, dup, err

def main():
    pat = load_pat()
    print(f"PAT loaded ({len(pat)} chars)")

    csv_dir = r"C:\Users\azfar\OneDrive\CC_Statement\firefly_csv"
    targets = sorted(glob.glob(os.path.join(csv_dir, "posb_*.csv")))
    overall = {"ok": 0, "dup": 0, "err": 0}
    for path in targets:
        if not os.path.exists(path):
            print(f"  SKIP (not found): {path}")
            continue
        ok, dup, err = import_file(path, pat)
        overall["ok"] += ok
        overall["dup"] += dup
        overall["err"] += err

    print()
    print(f"=== Grand total: ok={overall['ok']}  dup={overall['dup']}  err={overall['err']} ===")

if __name__ == "__main__":
    main()
