"""
firefly_setup_recurrences.py — One-time bootstrap of Firefly III recurring
transactions for every installment plan + revolving credit minimum payment.

Run once. Re-running creates duplicates — Firefly's API doesn't dedupe
recurrences by title. Use --dry-run first.

Each recurrence schedules a monthly transfer from POSB Savings to the
respective liability account, on its billing day, for the right number of
remaining months. Firefly's dashboard then projects monthly cashflow natively.

Usage:
  py scripts/firefly_setup_recurrences.py --dry-run
  py scripts/firefly_setup_recurrences.py            # creates the recurrences
"""
import os
import sys
import json
import argparse
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

FIREFLY_BASE = "http://127.0.0.1:8180"
PAT_FILE = Path(os.path.expandvars(r"%TEMP%\firefly_pat.txt"))
POSB_ID = 1  # POSB Savings asset account

# Plan list. Each entry creates ONE recurrence.
# (title, liability_acct_id, monthly_amount, billing_day, months_remaining, notes)
PLANS = [
    # ── Moneylenders ─────────────────────────────────────────────────────────
    ("EZ Loan repayment",            122, 498.72,  7, 8,  "Final payment 07-Jan-2027"),
    ("Lending Bee eGIRO",            123, 532.00, 28, 8,  "Monthly eGIRO, estimated 8 remaining"),

    # ── DBS Cashline (0% promo until Mar 2027) ───────────────────────────────
    ("DBS Cashline min payment",     100,  73.15,  5, 10, "0% promo until 16 Mar 2027; min payment 2.5%/$50"),

    # ── HSBC CC (revolving) ──────────────────────────────────────────────────
    ("HSBC CC min payment",          121,  68.13,  3, 12, "Revolving; min ~3% of outstanding ($2,270.98)"),

    # ── DBS CC — 6 active installment plans ──────────────────────────────────
    ("DBS CC plan 007 PPP24",        103,   9.30,  8, 2,  "Plan 007 — 2 remaining"),
    ("DBS CC plan 012 PMT-DEP",      103,  76.22,  8, 22, "Plan 012 — 22 remaining @ $76.22/mo"),
    ("DBS CC plan 013 PMT-DEP",      103,  62.50,  8, 22, "Plan 013 — 22 remaining @ $62.50/mo"),
    ("DBS CC plan 014 PMT-DEP",      103,  62.50,  8, 22, "Plan 014 — 22 remaining @ $62.50/mo"),
    ("DBS CC plan 015 PMT-DEP",      103,  41.66,  8, 22, "Plan 015 — 22 remaining @ $41.66/mo"),
    ("DBS CC plan 003IL (60M)",      103,  75.60,  8, 38, "Plan 003IL — 38 remaining @ $75.60/mo"),

    # ── SC CC — 5 EZBAL-EASYPAY plans on main card ───────────────────────────
    ("SC CC 36@#21 ($1,072)",        112,  75.10,  7, 15, "EZBAL 36mo plan #21, 15 remaining"),
    ("SC CC 60@#19 ($2,284)",        112,  72.00,  7, 41, "EZBAL 60mo plan #19, 41 remaining"),
    ("SC CC 60@#02 ($3,287)",        112,  80.70,  7, 58, "EZBAL 60mo plan #02, 58 remaining — longest tenor"),
    ("SC CC 60@#17 ($1,189)",        112,  36.17,  7, 43, "EZBAL 60mo plan #17, 43 remaining"),
    ("SC CC 60@#15 ($2,040)",        112,  59.98,  7, 45, "EZBAL 60mo plan #15, 45 remaining"),

    # ── SC Loan/BT ───────────────────────────────────────────────────────────
    ("SC Loan/BT 24@#15",            115,  93.88,  7, 9,  "EZBAL 24mo plan, 9 remaining"),

    # ── UOB CashPlus — 2 sub-loans ───────────────────────────────────────────
    ("UOB Personal Loan 27/60",      118,  59.70,  9, 33, "Loan 27/60 — 33 remaining @ $59.70/mo"),
    ("UOB Personal Loan 19/36",      118,  93.95,  9, 17, "Loan 19/36 — 17 remaining @ $93.95/mo"),

    # ── Maybank ──────────────────────────────────────────────────────────────
    ("Maybank CC FLEXICASH",         106, 106.97, 14, 59, "60-month FLEXICASH at $106.97/mo"),
    ("Maybank CreditAble Term Loan", 129, 105.00,  4, 59, "Term Loan 1/60 at $105/mo principal"),

    # ── GXS Loan ─────────────────────────────────────────────────────────────
    ("GXS Loan installment",         132, 2045.88, 30, 2, "2 remaining installments"),
]


def pat() -> str:
    return PAT_FILE.read_text(encoding="utf-8-sig").strip()


def next_billing_date(day: int) -> str:
    """Next future date with the given day-of-month. If today's date < day,
    use this month; else use next month."""
    today = date.today()
    if today.day < day:
        target = today.replace(day=min(day, 28))  # cap for short months
    else:
        # next month — handle Dec→Jan rollover
        year, month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        target = date(year, month, min(day, 28))
    return target.isoformat()


def add_months(d: date, n: int) -> date:
    """Add n months to date d, with day-of-month clamping."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return date(year, month, day)


def build_recurrence(plan) -> dict:
    title, liab_id, amount, billing_day, months, notes = plan
    first_date = next_billing_date(billing_day)
    fd_obj = date.fromisoformat(first_date)
    # Firefly caps nr_of_repetitions at 31; use repeat_until for any longer plans
    repeat_until = add_months(fd_obj, months - 1).isoformat()
    return {
        "title": title,
        "description": "Auto-generated recurring transfer for installment plan",
        "first_date": first_date,
        "repeat_until": repeat_until,
        "type": "transfer",
        "apply_rules": False,
        "active": True,
        "notes": notes,
        "repetitions": [{
            "type": "monthly",
            "moment": str(min(billing_day, 28)),
            "skip": 0,
            "weekend": 1,
        }],
        "transactions": [{
            "type": "transfer",
            "description": title,
            "amount": f"{amount:.2f}",
            "currency_code": "SGD",
            "source_id": str(POSB_ID),
            "destination_id": str(liab_id),
            "category_name": "Loan repayment",
        }],
    }


def post_recurrence(p: str, payload: dict) -> tuple[str, str]:
    req = urllib.request.Request(
        f"{FIREFLY_BASE}/api/v1/recurrences",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {p}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return ("ok", str(data.get("data", {}).get("id", "?")))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return ("err", f"HTTP {e.code}: {body[:300]}")
    except Exception as e:
        return ("err", str(e)[:200])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    p = pat()
    monthly_total = sum(plan[2] for plan in PLANS)
    print(f"Loaded {len(PLANS)} plans  |  total monthly obligation = SGD {monthly_total:,.2f}")
    print(f"Mode: {'DRY-RUN (no API calls)' if args.dry_run else 'LIVE'}")
    print()

    counts = {"ok": 0, "err": 0}
    for plan in PLANS:
        title, liab, amt, day, mos, notes = plan
        payload = build_recurrence(plan)
        first = payload["first_date"]
        if args.dry_run:
            print(f"  WOULD CREATE  {title:<40}  ${amt:>7.2f}/mo × {mos:>2} starting {first}  -> liability id={liab}")
            continue
        status, info = post_recurrence(p, payload)
        counts[status] += 1
        if status == "ok":
            print(f"  CREATED  id={info:<4}  {title:<40}  ${amt:>7.2f}/mo × {mos:>2} starting {first}")
        else:
            print(f"  FAILED   {title}  -> {info}")
    if not args.dry_run:
        print()
        print(f"Summary: ok={counts['ok']}  err={counts['err']}")


if __name__ == "__main__":
    main()
