"""ifast_email_parser.py — Idempotent iFAST Gmail → Firefly bridge.

Pulls every [G0050006] email in your Gmail (account-statement, dividend
notification, DIY/online transaction, confirmation note), parses the relevant
ones into structured records, and posts what's missing to Firefly.

Dedup strategy: each parsed event maps to a stable hash on
(date, fund, units, amount, kind). We tag every booked Firefly transaction
with `ifast-email:<hash>` so subsequent runs can `grep` Firefly via the
transactions search API and skip duplicates.

Supports:
  --dry-run        parse + classify + plan, no Firefly writes
  --since YYYY-MM-DD   only consider emails on/after this date
  --quiet          one-line summary only

Defaults to dry-run if no Firefly PAT is set.

Currently parses:
  - Dividend Notification (reinvested into the same fund)
  - Account Statement for <Month> <Year> (skipped — covered by buy/sell CSVs)
  - DIY transaction has been put into system (Buy/Sell — confirmation that
    a queued transaction completed; the actual record comes from the
    confirmation note + statement, but useful to surface)
  - Confirmation Note as at <date> (skipped — duplicates DIY entries)
  - Online transaction has been put into system / pending approval (skipped)
  - Buy Contract Voided (skipped — flagged in summary)
  - FATCA / Personal Particulars Updated (skipped — admin noise)
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_TOKEN_PATH = Path(r"C:\Users\azfar\metamcp-local\google-workspace-mcp\data\token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_QUERY = "G0050006"

FIREFLY_BASE = os.environ.get("FIREFLY_BASE_URL", "http://127.0.0.1:8180")
FIREFLY_PAT_PATH = Path(os.environ.get("TEMP", "")) / "firefly_pat.txt"
CPF_IS_ACCOUNT_ID = "147"
CPF_OA_ACCOUNT_ID = "141"


# ── Gmail helpers ────────────────────────────────────────────────────────────

def gmail_service():
    creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(svc, query: str, since: str | None = None):
    q = query
    if since:
        q = f"{query} after:{since.replace('-', '/')}"
    ids = []
    page = None
    while True:
        r = svc.users().messages().list(userId="me", q=q, maxResults=500, pageToken=page).execute()
        ids.extend(m["id"] for m in r.get("messages", []))
        page = r.get("nextPageToken")
        if not page:
            break
    return ids


def message_subject_and_body(svc, mid: str) -> tuple[str, str]:
    m = svc.users().messages().get(userId="me", id=mid, format="full").execute()
    headers = {h["name"]: h["value"] for h in m["payload"]["headers"]}
    subject = headers.get("Subject", "").strip()
    body = _walk_payload_for_text(m["payload"])
    if body and "<" in body:
        soup = BeautifulSoup(body, "html.parser")
        for tag in soup(["style", "script"]):
            tag.decompose()
        body = soup.get_text(separator=" ")
    body = re.sub(r"\s+", " ", body or "").strip()
    return subject, body


def _walk_payload_for_text(part) -> str | None:
    if part.get("mimeType", "").startswith("text/") and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", errors="replace")
    for sp in part.get("parts", []) or []:
        r = _walk_payload_for_text(sp)
        if r:
            return r
    return None


# ── Parsers ──────────────────────────────────────────────────────────────────

_DIVIDEND_PATTERNS = {
    "fund": re.compile(r"Name of Fund\s+(.+?)\s+(?:Ex Date|Dividend Rate)", re.I),
    "ex_date": re.compile(r"Ex Date\s+(\d{1,2}\s+\w{3}\s+\d{4})", re.I),
    "rate": re.compile(r"Dividend Rate\s+([A-Z]{3})\s+([0-9.]+)", re.I),
    "amount": re.compile(r"Dividend Amount\s+([A-Z]{3})\s+([0-9.,]+)", re.I),
    "reinvest_price": re.compile(r"Reinvestment Price\s+([A-Z]{3})\s+([0-9.]+)", re.I),
    "reinvest_units": re.compile(r"Reinvest Units\s+([0-9.,]+)", re.I),
    "payment_method": re.compile(r"Payment Method\s+([A-Z\-]+)", re.I),
}


def parse_dividend_email(body: str) -> dict | None:
    """Return a structured dividend record or None if the body isn't recognisable."""
    out = {}
    for key, pat in _DIVIDEND_PATTERNS.items():
        m = pat.search(body)
        if not m:
            return None
        if key in ("rate", "amount", "reinvest_price"):
            out[key + "_currency"] = m.group(1).upper()
            out[key] = float(m.group(2).replace(",", ""))
        elif key in ("reinvest_units",):
            out[key] = float(m.group(1).replace(",", ""))
        elif key == "ex_date":
            out[key] = datetime.strptime(m.group(1).strip(), "%d %b %Y").date().isoformat()
        else:
            out[key] = m.group(1).strip()
    return out


def event_hash(kind: str, date: str, fund: str, units: float, amount: float) -> str:
    h = hashlib.sha256(f"{kind}|{date}|{fund}|{units:.4f}|{amount:.2f}".encode()).hexdigest()[:16]
    return f"ifast-email:{h}"


# ── Firefly client ───────────────────────────────────────────────────────────

class Firefly:
    def __init__(self, base_url: str, pat: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {pat}"
        self.s.headers["Accept"] = "application/json"
        self.s.headers["Content-Type"] = "application/json"

    def find_by_tag(self, tag: str) -> bool:
        r = self.s.get(f"{self.base}/api/v1/search/transactions",
                       params={"query": f"tag:\"{tag}\"", "limit": 1}, timeout=20)
        if r.status_code != 200:
            return False
        return bool(r.json().get("data"))

    def find_on_account(self, account_id: str, date: str, amount: float,
                        tolerance: float = 0.01, window_days: int = 30) -> bool:
        """Cross-source dedup: same amount on destination account within ±window_days.

        Why a window? Email Ex Date and CSV Transaction Date (unit-allocation date)
        are typically ~2 weeks apart for the same dividend event. Exact-date match
        misses them. Match on (fund inferred by amount) + (date window) instead.
        """
        from datetime import datetime, timedelta
        d = datetime.fromisoformat(date)
        start = (d - timedelta(days=window_days)).date().isoformat()
        end = (d + timedelta(days=window_days)).date().isoformat()
        r = self.s.get(f"{self.base}/api/v1/accounts/{account_id}/transactions",
                       params={"start": start, "end": end, "limit": 200}, timeout=20)
        if r.status_code != 200:
            return False
        for t in r.json().get("data", []):
            tx = t["attributes"]["transactions"][0]
            if str(tx.get("destination_id")) != str(account_id):
                continue
            if abs(float(tx["amount"]) - amount) <= tolerance:
                return True
        return False

    def post_transaction(self, tx: dict) -> tuple[int, str]:
        r = self.s.post(f"{self.base}/api/v1/transactions",
                        json={"transactions": [tx], "error_if_duplicate_hash": True}, timeout=30)
        if r.status_code == 200:
            return 200, r.json()["data"]["id"]
        return r.status_code, r.text[:300]


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Plan only, no Firefly writes.")
    ap.add_argument("--since", help="YYYY-MM-DD, restrict to emails on/after.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    pat = FIREFLY_PAT_PATH.read_text(encoding="utf-8").strip() if FIREFLY_PAT_PATH.exists() else ""
    if not pat and not args.dry_run:
        print(f"No Firefly PAT at {FIREFLY_PAT_PATH}; forcing --dry-run.", file=sys.stderr)
        args.dry_run = True
    ff = Firefly(FIREFLY_BASE, pat) if pat else None

    svc = gmail_service()
    ids = list_message_ids(svc, GMAIL_QUERY, since=args.since)
    if not args.quiet:
        print(f"[gmail] {len(ids)} message(s) matched query '{GMAIL_QUERY}'"
              f"{f' since {args.since}' if args.since else ''}.")

    seen_kinds = Counter()
    plan = []          # list of (kind, hash, payload, tx_dict)
    skipped_unknown = []

    for i, mid in enumerate(ids):
        try:
            subj, body = message_subject_and_body(svc, mid)
        except Exception as e:
            print(f"  [warn] failed to read {mid}: {e}", file=sys.stderr)
            continue
        kind = classify_subject(subj)
        seen_kinds[kind] += 1
        if kind != "dividend":
            continue
        rec = parse_dividend_email(body)
        if not rec:
            skipped_unknown.append((mid, subj))
            continue

        h = event_hash("dividend", rec["ex_date"], rec["fund"], rec["reinvest_units"], rec["amount"])
        # Skip if already in Firefly — TWO checks:
        # 1. Own tag (handles re-runs of this parser)
        # 2. Cross-source by date+amount on the CPF-IS account (handles CSV-imported entries)
        if ff and ff.find_by_tag(h):
            continue
        if ff and ff.find_on_account(CPF_IS_ACCOUNT_ID, rec["ex_date"], rec["amount"]):
            continue

        short_fund = rec["fund"].replace(" (formerly Nikko AM)", "")
        notes = (
            f"Fund: {rec['fund']}\n"
            f"Units: {rec['reinvest_units']}\n"
            f"Price: {rec['reinvest_price_currency']} {rec['reinvest_price']}\n"
            f"Payment method: {rec['payment_method']}\n"
            f"Source: iFAST Gmail dividend notification ({h})\n"
            f"Gmail message id: {mid}"
        )
        tx = {
            "type": "deposit",
            "date": rec["ex_date"],
            "amount": f"{rec['amount']:.2f}",
            "description": f"Dividend — {short_fund} ({rec['reinvest_units']} units @ {rec['reinvest_price']})",
            "source_name": "iFAST Dividends (CPFIS-OA)",
            "destination_id": CPF_IS_ACCOUNT_ID,
            "category_name": "Investment Income",
            "notes": notes,
            "tags": ["cpf-is", "ifast", "dividend-reinvest", "from-email", h,
                     f"y{rec['ex_date'][:4]}"],
        }
        plan.append(("dividend", h, rec, tx))

    if not args.quiet:
        print(f"[classify] {dict(seen_kinds)}")
        print(f"[plan] {len(plan)} new dividend(s) to book.")
        if skipped_unknown:
            print(f"[warn] {len(skipped_unknown)} dividend-subject email(s) failed to parse:")
            for mid, subj in skipped_unknown[:5]:
                print(f"  - {mid}  {subj[:80]}")

    booked = 0
    for kind, h, rec, tx in plan:
        if args.dry_run:
            print(f"  [DRY] would book {kind} {rec['ex_date']} "
                  f"{rec['fund'][:38]:38} {rec['amount']:>8.2f}  hash={h}")
            continue
        sc, body = ff.post_transaction(tx)
        if sc == 200:
            booked += 1
            print(f"  OK  tx#{body}  {rec['ex_date']}  {rec['amount']:>8.2f}  {rec['fund'][:40]}  hash={h}")
        else:
            print(f"  FAIL [{sc}]  {rec['ex_date']}  {rec['fund']}: {body[:200]}", file=sys.stderr)

    if args.quiet:
        print(f"ifast_email_parser: scanned {len(ids)}, planned {len(plan)}, booked {booked}, dry_run={args.dry_run}")
    else:
        print(f"\n[done] scanned {len(ids)}, planned {len(plan)}, booked {booked}, dry_run={args.dry_run}")


def classify_subject(subj: str) -> str:
    s = subj.lower()
    if "dividend notification" in s:
        return "dividend"
    if "account statement" in s:
        return "statement"
    if "diy transaction" in s:
        return "diy_txn"
    if "online transaction" in s:
        return "online_txn"
    if "confirmation note" in s:
        return "confirm_note"
    if "buy contract voided" in s:
        return "voided"
    if "fatca" in s or "personal particulars" in s or "verify your email" in s:
        return "admin"
    return "other"


if __name__ == "__main__":
    main()
