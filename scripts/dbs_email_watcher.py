"""
dbs_email_watcher.py — Gmail-driven auto-import of DBS transaction alerts to Firefly III.

Reads emails from ibanking.alert@dbs.com and DBSeAdvice@dbs.com, parses the
transaction type / amount / counterparty, and posts to Firefly. Labels processed
messages "POSB-imported" so re-runs skip them.

Counterparty mapping table at the top of the file — extend as you see new
counterparties in your Telegram error notifications.

Cron: every 5 minutes via scripts/dbs_email_watcher.ps1 -> Scheduled Task.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Config ───────────────────────────────────────────────────────────────────
CREDS_DIR = Path(r"C:\Users\azfar\metamcp-local\google-workspace-mcp\data")
CREDENTIALS_JSON = CREDS_DIR / "credentials.json"
TOKEN_JSON = CREDS_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

FIREFLY_BASE = "http://127.0.0.1:8180"
FIREFLY_PAT_FILE = Path(os.path.expandvars(r"%TEMP%\firefly_pat.txt"))

PROCESSED_LABEL = "POSB-imported"
GMAIL_QUERY = "from:(ibanking.alert@dbs.com OR DBSeAdvice@dbs.com)"
MAX_MESSAGES_PER_RUN = 50
# How far back to look on each run. First run should use a small window
# (e.g. "1d") to avoid mass-importing months of historical emails — most are
# already in Firefly via PDF/CSV imports. Set NEWER_THAN env var to override.
NEWER_THAN = os.environ.get("DBS_WATCHER_NEWER_THAN", "1d")

# POSB asset account in Firefly
POSB_ACCT_ID = 1
POSB_ACCT_NAME = "POSB Savings"

# Counterparty mapping: substring (case-insensitive) → routing rule.
# transfer_to_account_id: post as transfer from POSB to that liability/asset account
# revenue_source / expense_destination: post as deposit/withdrawal with a named opposite party
COUNTERPARTY_MAP = {
    "EZ LOAN PTE.LTD":         {"kind": "transfer", "account_id": 122, "category": "Loan repayment"},
    "LENDING BEE":             {"kind": "transfer", "account_id": 123, "category": "Loan repayment"},
    "HENDERSON SECURITY":      {"kind": "deposit",  "source_name": "<HSS Salary>", "category": "Salary"},
    "WISE":                    {"kind": "withdrawal", "destination_name": "Wise (top-up)", "category": "FX/Transfer"},
    "COINBASE":                {"kind": "transfer", "account_id": 97, "category": "Crypto deposit"},   # Coinbase Account
    "CRYPTO.COM":              {"kind": "transfer", "account_id": 98, "category": "Crypto deposit"},
    "SINGAPORE LIFE":          {"kind": "withdrawal", "destination_name": "Singlife", "category": "Insurance"},
    "QASHIER":                 {"kind": "withdrawal", "destination_name": "Qashier merchant", "category": "Food/Retail"},
    "EZ-LINK":                 {"kind": "withdrawal", "destination_name": "EZ-Link top-up", "category": "Transport"},
    "GRAB":                    {"kind": "withdrawal", "destination_name": "Grab", "category": "Transport"},
}


# ── Email parsing patterns ───────────────────────────────────────────────────
# DBS sends ~5 distinct email types. We classify by SUBJECT first, then run
# type-specific body regex. Non-transactional emails are explicitly skipped.

# Map "Other bank's card ending NNNN" → Firefly liability account id (your cards
# with the last 4 digits). Extend as new cards land.
CARD_LAST4 = {
    "5159": {"acct_id": 121, "name": "HSBC CC"},
    "5959": {"acct_id": 112, "name": "SC CC"},      # 5498-...-5959? verify
    # Add more as you encounter them in DBS bill-payment / external-card emails
}

REF_RE   = re.compile(r"Transaction Ref[:\s]+([A-Za-z0-9]+)")
# Two amount formats DBS uses: "SGD123.45" / "SGD 123.45" / "S$123.45"
AMOUNT_RE = re.compile(r"\b(?:SGD|S\$)\s*([\d,]+\.\d{2})", re.IGNORECASE)
TIME_RE  = re.compile(r"(?:Date\s*(?:and|&)?\s*Time)[:\s]+(\d{1,2}\s+\w+(?:\s+\d{4})?\s+\d{1,2}:\d{2})", re.IGNORECASE)
DATE_RE  = re.compile(r"\b(?:dated|on)\s+(\d{1,2}\s+\w+(?:\s+\d{4})?)\b", re.IGNORECASE)

# "From: X To: Y" extraction — REQUIRE the literal colon and case-sensitive
# match (avoid "to" in prose like "refer to your PAYNOW")
TO_RE   = re.compile(r"(?:^|\s)To:\s+(.+?)(?=\s+(?:If unauthorised|Didn|If this|To view|For enquiries|Thank you|$))", re.DOTALL)
FROM_RE = re.compile(r"(?:^|\s)From:\s+(.+?)(?=\s+To:|\s+(?:If unauthorised|To view|For enquiries|Thank you|$))", re.DOTALL)
# For external-bank-card payments: "Other bank's card ending NNNN"
EXT_CARD_RE = re.compile(r"Other bank.{0,5}s? card ending\s+(\d{4})", re.IGNORECASE)


def classify_subject(subject: str) -> str | None:
    """Return a TYPE token, or None to skip."""
    s = subject.lower()
    # Explicit skips
    if any(k in s for k in ["edocument", "contact details", "manage alert",
                              "successful login", "request received"]):
        return "skip"
    # Transactional types
    if "received a transfer" in s:           return "transfer_in"
    if "nets scan" in s:                      return "nets"
    if "successful bill payment" in s:        return "bill_payment"
    if "payment to another" in s:             return "external_card"
    if "ibanking alerts" in s:                return "ibanking"   # need body check
    if "successful payment" in s:             return "generic_payment"
    return "skip"  # unknown subjects are safer to skip than mis-import


# ── Gmail helpers ────────────────────────────────────────────────────────────
def gmail_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_JSON), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_JSON.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_or_create_label(svc, name: str) -> str:
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for l in labels:
        if l["name"] == name:
            return l["id"]
    new = svc.users().labels().create(userId="me", body={
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }).execute()
    return new["id"]


def decode_body(payload: dict) -> str:
    """Walk multipart preferring text/plain. HTML fallback uses BeautifulSoup
    to strip styles/scripts/hidden preheaders that regex misses."""
    plain_chunks = []
    html_chunks = []

    def walk(p):
        if "parts" in p:
            for sp in p["parts"]:
                walk(sp)
        data = p.get("body", {}).get("data")
        if not data:
            return
        try:
            txt = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:
            return
        mt = p.get("mimeType", "")
        if mt == "text/plain":
            plain_chunks.append(txt)
        elif mt == "text/html":
            html_chunks.append(txt)

    walk(payload)

    if plain_chunks:
        body = "\n".join(plain_chunks)
    else:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("\n".join(html_chunks), "html.parser")
        # Drop style/script entirely
        for tag in soup(["style", "script"]):
            tag.decompose()
        # Drop hidden preheader divs (display:none, mso-hide, max-height:0, etc.)
        for tag in soup.find_all(style=True):
            style = (tag.get("style") or "").lower()
            if ("display:none" in style.replace(" ", "")
                or "max-height:0" in style.replace(" ", "")
                or "mso-hide:all" in style.replace(" ", "")
                or "visibility:hidden" in style.replace(" ", "")):
                tag.decompose()
        body = soup.get_text(separator=" ")

    body = re.sub(r"\s+", " ", body)
    return body.strip()


# ── Firefly helpers ──────────────────────────────────────────────────────────
def firefly_pat() -> str:
    return FIREFLY_PAT_FILE.read_text(encoding="utf-8-sig").strip()


def post_firefly_tx(pat: str, tx: dict, dedupe_note: str) -> tuple[str, str]:
    """Returns (status, info). status = 'ok' | 'dup' | 'err'.
    Set DBS_WATCHER_BYPASS_DUP=1 to override Firefly's hash-dedup (useful when
    rebuilding after wrong imports were soft-deleted)."""
    bypass = os.environ.get("DBS_WATCHER_BYPASS_DUP", "0") == "1"
    payload = {
        "error_if_duplicate_hash": not bypass,
        "apply_rules": True,
        "fire_webhooks": False,
        "group_title": None,
        "transactions": [tx],
    }
    req = urllib.request.Request(
        f"{FIREFLY_BASE}/api/v1/transactions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
            return ("ok", str(body.get("data", {}).get("id", "?")))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "Duplicate" in body or "duplicate" in body:
            return ("dup", body[:200])
        return ("err", f"HTTP {e.code}: {body[:300]}")
    except Exception as e:
        return ("err", str(e)[:200])


# ── Email → transaction classifier ───────────────────────────────────────────
def classify_email(subject: str, body: str) -> dict | None:
    """Subject-driven dispatch. Return dict describing the transaction, or None."""
    typ = classify_subject(subject)
    if typ == "skip" or typ is None:
        return None

    # iBanking Alerts: only transactional if body mentions PAYNOW or FAST transfer
    if typ == "ibanking":
        b = body.lower()
        if "your paynow dated" in b or "fast interbank funds transfer" in b:
            pass  # is transactional
        else:
            return None  # Manage Alert / eGIRO setup / other admin

    amt_m = AMOUNT_RE.search(body)
    if not amt_m:
        return None
    amount = float(amt_m.group(1).replace(",", ""))

    ref_m = REF_RE.search(body)
    ref = ref_m.group(1) if ref_m else ""

    # Direction by type
    incoming = (typ == "transfer_in")

    # Counterparty extraction — type-specific
    counterparty = "Unknown"
    if typ == "transfer_in":
        m = FROM_RE.search(body)
        if m: counterparty = m.group(1).strip()
    elif typ == "external_card":
        m = EXT_CARD_RE.search(body)
        if m:
            last4 = m.group(1)
            mapped = CARD_LAST4.get(last4)
            counterparty = mapped["name"] if mapped else f"Other bank card ending {last4}"
        else:
            counterparty = "Other bank card (unrecognised)"
    elif typ == "bill_payment":
        # "To: VISA Platinum (Ref ending 2424)" — extract the part name + last 4
        m = TO_RE.search(body)
        if m:
            counterparty = m.group(1).strip()[:80]
    elif typ in ("nets", "ibanking", "generic_payment"):
        m = TO_RE.search(body)
        if m: counterparty = m.group(1).strip()[:80]

    counterparty = counterparty.rstrip(" .,;-")

    # Map to routing rule (substring match on counterparty)
    rule = None
    for needle, r in COUNTERPARTY_MAP.items():
        if needle.upper() in counterparty.upper():
            rule = r
            break

    # Date: prefer Time field, else "dated" field, else today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tx_date = today
    time_m = TIME_RE.search(body)
    date_m = DATE_RE.search(body)
    raw_date = (time_m.group(1) if time_m else (date_m.group(1) if date_m else None))
    if raw_date:
        try:
            # Best effort: try a few common formats
            for fmt in ("%d %B %Y %H:%M", "%d %b %Y %H:%M", "%d %B %H:%M", "%d %b %H:%M", "%d %B %Y", "%d %b %Y", "%d %B", "%d %b"):
                try:
                    parsed = datetime.strptime(raw_date, fmt)
                    # If no year, assume current year
                    if parsed.year < 2000:
                        parsed = parsed.replace(year=datetime.now().year)
                    tx_date = parsed.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    return {
        "incoming": incoming,
        "amount": amount,
        "counterparty": counterparty,
        "rule": rule,
        "ref": ref,
        "date": tx_date,
    }


def build_firefly_tx(parsed: dict, gmail_msg_id: str) -> dict:
    """Build the Firefly transaction payload from parsed email."""
    amount = parsed["amount"]
    cp = parsed["counterparty"]
    rule = parsed["rule"]
    ref = parsed["ref"]
    date = parsed["date"]
    notes_tail = f"\n\n[auto-imported from DBS email | msgid={gmail_msg_id} | ref={ref}]"

    if parsed["incoming"]:
        # Deposit into POSB
        if rule and rule.get("kind") == "deposit":
            return {
                "type": "deposit",
                "date": date,
                "amount": f"{amount:.2f}",
                "description": f"Incoming: {cp}",
                "source_name": rule["source_name"],
                "destination_id": POSB_ACCT_ID,
                "category_name": rule.get("category", ""),
                "notes": notes_tail.strip(),
            }
        return {
            "type": "deposit",
            "date": date,
            "amount": f"{amount:.2f}",
            "description": f"Incoming: {cp}",
            "source_name": cp or "Unknown",
            "destination_id": POSB_ACCT_ID,
            "notes": notes_tail.strip(),
        }

    # Outgoing
    if rule and rule["kind"] == "transfer":
        return {
            "type": "transfer",
            "date": date,
            "amount": f"{amount:.2f}",
            "description": f"Payment to {cp}",
            "source_id": POSB_ACCT_ID,
            "destination_id": rule["account_id"],
            "category_name": rule.get("category", ""),
            "notes": notes_tail.strip(),
        }
    if rule and rule["kind"] == "withdrawal":
        return {
            "type": "withdrawal",
            "date": date,
            "amount": f"{amount:.2f}",
            "description": f"Payment to {cp}",
            "source_id": POSB_ACCT_ID,
            "destination_name": rule["destination_name"],
            "category_name": rule.get("category", ""),
            "notes": notes_tail.strip(),
        }
    # Unknown counterparty — default withdrawal to a named destination
    return {
        "type": "withdrawal",
        "date": date,
        "amount": f"{amount:.2f}",
        "description": f"Payment to {cp}",
        "source_id": POSB_ACCT_ID,
        "destination_name": cp or "Unknown",
        "notes": notes_tail.strip() + "\n[NOTE: no counterparty rule matched — review and add to COUNTERPARTY_MAP]",
    }


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    pat = firefly_pat()
    svc = gmail_service()
    label_id = get_or_create_label(svc, PROCESSED_LABEL)

    # Search: DBS emails NOT yet labeled processed, within the lookback window
    q = f"{GMAIL_QUERY} -label:{PROCESSED_LABEL.replace(' ', '-')} newer_than:{NEWER_THAN}"
    print(f"Gmail query: {q}")
    resp = svc.users().messages().list(userId="me", q=q, maxResults=MAX_MESSAGES_PER_RUN).execute()
    messages = resp.get("messages", [])
    print(f"Found {len(messages)} candidate DBS message(s)")

    counts = {"ok": 0, "dup": 0, "err": 0, "skip": 0, "unknown_cp": 0}
    unknown_cps = []

    for m in messages:
        msg_id = m["id"]
        full = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        body = decode_body(full["payload"])

        parsed = classify_email(subject, body)
        if not parsed:
            print(f"  SKIP  {msg_id}  subject={subject[:60]!r}")
            counts["skip"] += 1
            svc.users().messages().modify(userId="me", id=msg_id,
                                          body={"addLabelIds": [label_id]}).execute()
            continue

        if parsed["rule"] is None:
            counts["unknown_cp"] += 1
            unknown_cps.append((parsed["counterparty"], parsed["amount"], msg_id))

        tx = build_firefly_tx(parsed, msg_id)
        status, info = post_firefly_tx(pat, tx, f"msgid={msg_id}")
        counts[status] += 1
        direction = "IN " if parsed["incoming"] else "OUT"
        print(f"  {status.upper():4}  {direction}  SGD {parsed['amount']:>8.2f}  "
              f"{parsed['counterparty'][:40]:<40}  msgid={msg_id}  -> {info[:60]}")

        if status == "ok":
            # Apply label so we don't reprocess
            svc.users().messages().modify(userId="me", id=msg_id,
                                          body={"addLabelIds": [label_id]}).execute()

    print()
    print(f"Summary: ok={counts['ok']}  dup={counts['dup']}  err={counts['err']}  "
          f"skipped={counts['skip']}  unknown_counterparty={counts['unknown_cp']}")
    if unknown_cps:
        print("\nUnknown counterparties (add to COUNTERPARTY_MAP):")
        for cp, amt, mid in unknown_cps[:10]:
            print(f"  {cp:<50}  SGD {amt:>8.2f}  msg={mid}")

    return counts


if __name__ == "__main__":
    try:
        counts = main()
        sys.exit(0 if counts["err"] == 0 else 1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(2)
