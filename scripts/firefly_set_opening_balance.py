"""
Create a single opening-balance transaction for POSB Savings in Firefly III.
Opening balance: SGD 2,338.06 as of 2023-12-31 (Balance Brought Forward, Jan 2024 statement).
Counter account: <Historical Net Asset>
"""
import json
import os
import urllib.request
import urllib.error

PAT_PATH = os.path.expandvars(r"%TEMP%\firefly_pat.txt")
BASE_URL = "http://127.0.0.1:8180"

def load_pat():
    with open(PAT_PATH, encoding="utf-8-sig") as f:
        return f.read().strip()

def main():
    pat = load_pat()
    print(f"PAT loaded ({len(pat)} chars)")

    payload = {
        "error_if_duplicate_hash": True,
        "apply_rules": False,
        "fire_webhooks": False,
        "group_title": None,
        "transactions": [{
            "type": "deposit",
            "date": "2023-12-31",
            "amount": "2338.06",
            "description": "Opening Balance",
            "source_name": "<Historical Net Asset>",
            "destination_name": "POSB Savings",
        }],
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
            body = resp.read().decode()
            data = json.loads(body)
            txn_id = data.get("data", {}).get("id", "?")
            print(f"Created: transaction id={txn_id}, date=2023-12-31, amount=SGD 2338.06")
            print(f"  source=<Historical Net Asset> -> destination=POSB Savings")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "Duplicate" in body or "duplicate" in body:
            print("Already exists (duplicate) — nothing to do.")
        else:
            print(f"ERROR HTTP {e.code}: {body[:400]}")
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    main()
