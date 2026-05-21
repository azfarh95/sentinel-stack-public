"""CC Statement Auto-Matcher / Commitment Reconciler.

For each CC statement:
  1. Extract: statement_date, total_due, minimum_due, payment_due_date
  2. Persist as a row in `cc_statement_commitment` table
  3. Match against POSB outflows within ±10 days of due_date for the same amount
  4. Mark status: pending / matched_on_time / matched_late / overdue

Run:
    docker exec portfolio-mcp python -m app.cc_commitment_tracker --scan
    docker exec portfolio-mcp python -m app.cc_commitment_tracker --match
    docker exec portfolio-mcp python -m app.cc_commitment_tracker --status
"""
from __future__ import annotations
import argparse
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app import database as db
from app.universal_pdf_parser import extract_text

# Card families — file glob, CoA, name, header regexes
CC_FAMILIES = [
    {
        "code": "dbs-cc", "coa": "2111", "name": "DBS Live Fresh",
        "globs": ["DBS CC*.pdf", "Credit Cards Consolidated*.pdf"],
        # DBS layout: 4 fields on the values line:
        # "15 Mar 2026  $9,100.00  $50.00  09 Apr 2026"
        # Use ONE combined regex to capture all 4 atomically.
        "combined_re": r"(\d{1,2}\s+\w{3}\s+\d{4})\s+\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})\s+(\d{1,2}\s+\w{3}\s+\d{4})",
        "combined_groups": {"stmt_date": 1, "credit_limit": 2, "min_due": 3, "due_date": 4},
        "total_due_re": r"Total Outstanding Balance.*?(?:\$|\s)([\d,]+\.\d{2})",
    },
    {
        "code": "maybank-cc", "coa": "2112", "name": "Maybank Platinum Visa",
        "globs": ["Platinum Visa Card*.pdf"],
        "stmt_date_re": r"Statement Date\s*:\s*(\d{2}/\d{2}/\d{4})",
        "due_date_re":  r"Due Date\s*:\s*(\d{2}/\d{2}/\d{4})",
        "total_due_re": r"Total Due\s*:\s*([\d,]+\.\d{2})",
        "min_due_re":   r"Minimum Due\s*:\s*([\d,]+\.\d{2})",
    },
    {
        "code": "hsbc-cc", "coa": "2114", "name": "HSBC Visa Revolution",
        "globs": ["HSBC CC*.pdf", "HSBC Jan*.pdf", "HSBC Feb*.pdf", "HSBC Mar*.pdf"],
        # HSBC OCR'd format:
        # "From 16 MAR 2026 to 14 APR 2026 | 2,390.98 118.86 04 May 2026"
        # Combined: period_to_date | total_due  min_payment  due_date
        "combined_re": r"to\s+(\d{1,2}\s+\w{3}\s+\d{4})\s*\|?\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(\d{1,2}\s+\w{3}\s+\d{4})",
        "combined_groups": {"stmt_date": 1, "total_due": 2, "min_due": 3, "due_date": 4},
        "requires_ocr": True,
    },
]


CC_ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")


def parse_date(s: str | None) -> str | None:
    """Try multiple date formats. Return ISO string or None."""
    if not s: return None
    s = s.strip()
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(s: str | None) -> float | None:
    if not s: return None
    try: return float(s.replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError): return None


def ensure_table(s):
    """Create cc_statement_commitment table if missing.
    Adds cumulative-payment + interest tracking columns (idempotent)."""
    s.execute(text("""
      CREATE TABLE IF NOT EXISTS cc_statement_commitment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_coa VARCHAR NOT NULL,
        card_name VARCHAR,
        statement_date DATE NOT NULL,
        total_due FLOAT,
        minimum_due FLOAT,
        payment_due_date DATE,
        matched_payment_jid INTEGER,
        matched_at DATETIME,
        match_amount FLOAT,
        days_offset INTEGER,
        status VARCHAR DEFAULT 'pending',
        source_doc VARCHAR,
        source_ref VARCHAR,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(card_coa, statement_date)
      )
    """))
    # Add new columns if not present (SQLite idempotent ADD COLUMN)
    for col_def in [
        "cumulative_paid FLOAT DEFAULT 0",
        "unpaid_balance FLOAT",
        "payments_jids VARCHAR",                # comma-separated list of matched jids
        "annual_interest_rate FLOAT",            # e.g. 0.2780
        "estimated_interest FLOAT",
        "interest_warning VARCHAR",
    ]:
        col_name = col_def.split()[0]
        try:
            s.execute(text(f"ALTER TABLE cc_statement_commitment ADD COLUMN {col_def}"))
        except Exception:
            pass   # column already exists
    s.commit()


# Annual interest rates per CC (decimal). Sourced from each statement's WARNING text.
CC_INTEREST_RATES = {
    "2111": 0.2780,   # DBS Live Fresh: 27.80% p.a.
    "2112": 0.2790,   # Maybank Platinum Visa: 27.90% p.a.
    "2113": 0.2780,   # SC Cashback: 27.80% p.a. (typical)
    "2114": 0.2780,   # HSBC Visa Revolution: 27.80% p.a. (purchases)
}


def scan_statements(s, since: date = date(2025, 1, 1)):
    """Walk all CC PDFs, extract commitment headers, insert/update rows."""
    discovered = 0
    inserted = 0
    updated = 0
    for fam in CC_FAMILIES:
        files = []
        for g in fam["globs"]:
            files.extend(CC_ROOT.rglob(g))
        files = sorted(set(files))
        print(f"\n=== {fam['code'].upper()}  CoA {fam['coa']}  ({len(files)} PDFs) ===")
        for pdf in files:
            try:
                text_content = extract_text(pdf, requires_ocr=fam.get("requires_ocr", False), max_pages=2)
            except Exception as e:
                print(f"  [parse err] {pdf.name}: {e}")
                continue

            stmt_iso = due_iso = total = min_due = None
            if "combined_re" in fam:
                m = re.search(fam["combined_re"], text_content, re.MULTILINE | re.DOTALL)
                if m:
                    g = fam["combined_groups"]
                    stmt_iso = parse_date(m.group(g["stmt_date"]))
                    due_iso = parse_date(m.group(g["due_date"]))
                    if "total_due" in g:
                        total = parse_amount(m.group(g["total_due"]))
                    if "min_due" in g:
                        min_due = parse_amount(m.group(g["min_due"]))
                # Fall back to total_due_re if combined didn't yield total
                if total is None and "total_due_re" in fam:
                    tm = re.search(fam["total_due_re"], text_content, re.MULTILINE | re.DOTALL)
                    if tm:
                        total = parse_amount(tm.group(1))
            else:
                stmt_m = re.search(fam.get("stmt_date_re", ""), text_content, re.MULTILINE | re.DOTALL)
                due_m = re.search(fam.get("due_date_re", ""), text_content, re.MULTILINE | re.DOTALL)
                total_m = re.search(fam.get("total_due_re", ""), text_content, re.MULTILINE | re.DOTALL)
                min_m = re.search(fam.get("min_due_re", ""), text_content, re.MULTILINE | re.DOTALL)
                stmt_iso = parse_date(stmt_m.group(1) if stmt_m else None)
                due_iso = parse_date(due_m.group(1) if due_m else None)
                total = parse_amount(total_m.group(1) if total_m else None)
                min_due = parse_amount(min_m.group(1) if min_m else None)

            if not stmt_iso or stmt_iso < since.isoformat():
                continue
            discovered += 1

            # Insert / update
            existing = s.execute(text("""
              SELECT id FROM cc_statement_commitment
              WHERE card_coa=:c AND statement_date=:d
            """), {"c": fam["coa"], "d": stmt_iso}).fetchone()
            params = {
                "card_coa": fam["coa"], "card_name": fam["name"],
                "statement_date": stmt_iso, "total_due": total,
                "minimum_due": min_due, "payment_due_date": due_iso,
                "source_doc": "CC_PDF", "source_ref": pdf.name[:80],
                "status": "pending",
            }
            if existing:
                s.execute(text("""
                  UPDATE cc_statement_commitment
                  SET total_due=:total_due, minimum_due=:minimum_due,
                      payment_due_date=:payment_due_date, source_ref=:source_ref,
                      updated_at=CURRENT_TIMESTAMP
                  WHERE id=:id
                """), {**params, "id": existing[0]})
                updated += 1
                op = "↻"
            else:
                s.execute(text("""
                  INSERT INTO cc_statement_commitment
                  (card_coa, card_name, statement_date, total_due, minimum_due,
                   payment_due_date, status, source_doc, source_ref)
                  VALUES (:card_coa, :card_name, :statement_date, :total_due,
                          :minimum_due, :payment_due_date, :status,
                          :source_doc, :source_ref)
                """), params)
                inserted += 1
                op = "+"
            total_str = f"${total:>10,.2f}" if total else "        ?  "
            min_str = f"${min_due:>8,.2f}" if min_due else "      ?  "
            print(f"  {op} {fam['code']}  stmt={stmt_iso}  due={due_iso}  total={total_str}  min={min_str}")
    s.commit()
    print(f"\nDiscovered {discovered} statements ({inserted} new, {updated} updated)")
    return discovered, inserted, updated


def run_matcher(s, window_days: int = 10):
    """For each pending commitment, sum ALL POSB outflows to the CC in the billing
    cycle window, then compare against total_due AND minimum_due.

    Status logic (CC reality):
      cumulative_paid >= total_due  → matched_fully    (no interest)
      cumulative_paid >= minimum_due → matched_partial (BEARS 27.8% pa on unpaid)
      cumulative_paid > 0           → underpaid        (interest + late fee risk)
      cumulative_paid == 0          → overdue          (late fee certain)
    """
    pending = s.execute(text("""
      SELECT id, card_coa, card_name, statement_date, total_due, minimum_due, payment_due_date
      FROM cc_statement_commitment
      WHERE status NOT IN ('archived')
        AND total_due IS NOT NULL AND payment_due_date IS NOT NULL
      ORDER BY payment_due_date
    """)).all()
    print(f"Matching {len(pending)} commitments (cumulative-payment mode)…\n")
    fully = partial = under = overdue_n = 0
    for r in pending:
        cid, coa, name, sd, total, min_due, due_d = r
        if total is None or total < 0.01:
            continue
        stmt_date = datetime.fromisoformat(str(sd)[:10]).date()
        due_date = datetime.fromisoformat(str(due_d)[:10]).date()
        # WINDOW: statement_date to next-expected-statement-date (~33d).
        window_start = stmt_date.isoformat()
        window_end = (stmt_date + timedelta(days=33)).isoformat()
        # Find POSB→CC outflow within window matching total_due
        # Sum ALL payments to this CC within window — cumulative
        rows = s.execute(text("""
          SELECT j.id, j.journal_date, j.source_doc, gl.debit
          FROM journals j
          JOIN general_ledger gl ON gl.journal_id = j.id
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE coa.account_code = :coa
            AND gl.debit > 0
            AND j.journal_date BETWEEN :ws AND :we
            AND j.status = 'posted'
            AND j.source_doc NOT LIKE 'FIREFLY_BRIDGE%'  -- exclude old bridge dups
          ORDER BY j.journal_date
        """), {"coa": coa, "ws": window_start, "we": window_end}).all()

        cumulative_paid = sum(float(r[3] or 0) for r in rows)
        jids = ",".join(str(r[0]) for r in rows) if rows else None
        unpaid = max(0.0, total - cumulative_paid)
        today = date.today()
        days_past_due = max(0, (today - due_date).days)
        annual_rate = CC_INTEREST_RATES.get(coa, 0.2780)
        # Daily interest accrues from the day after due_date on unpaid_balance
        estimated_interest = 0.0
        if unpaid > 0.01 and today > due_date:
            estimated_interest = unpaid * annual_rate * (days_past_due / 365.0)

        # Decide status
        warning = None
        if cumulative_paid + 0.01 >= total:
            status = "matched_fully"
            fully += 1
            symbol = "✓"
            note = f"paid ${cumulative_paid:,.2f} in full ({len(rows)} payments)"
        elif min_due and cumulative_paid + 0.01 >= min_due:
            status = "matched_partial"
            partial += 1
            symbol = "◐"
            warning = f"⚠ INTEREST ACCRUING @ {annual_rate*100:.2f}% p.a. on ${unpaid:,.2f} unpaid"
            note = f"paid ${cumulative_paid:,.2f} of ${total:,.2f} (min ${min_due:,.2f} met) — interest on ${unpaid:,.2f}"
        elif cumulative_paid > 0.01:
            status = "underpaid"
            under += 1
            symbol = "⚠"
            warning = f"⚠ UNDERPAID — likely late fee + interest @ {annual_rate*100:.2f}% p.a."
            note = f"paid ${cumulative_paid:,.2f} of ${total:,.2f} — below min ${min_due or 0:,.2f}"
        elif today <= due_date:
            status = "upcoming" if today < due_date else "due_today"
            symbol = "⏳"
            note = f"upcoming — ${total:,.2f} due by {due_d}"
        elif days_past_due <= window_days:
            status = "pending"
            symbol = "?"
            note = f"recently due ({days_past_due}d ago) — no payment yet, may still be in transit"
        else:
            status = "overdue"
            overdue_n += 1
            symbol = "⚠"
            warning = f"⚠ OVERDUE {days_past_due}d — late fee + interest @ {annual_rate*100:.2f}% p.a. on ${unpaid:,.2f}"
            note = f"no payment found in window"

        s.execute(text("""
          UPDATE cc_statement_commitment
          SET cumulative_paid=:cp, unpaid_balance=:ub, payments_jids=:jids,
              annual_interest_rate=:rate, estimated_interest=:ei,
              interest_warning=:warn, status=:st, updated_at=CURRENT_TIMESTAMP,
              matched_payment_jid = CASE WHEN :first_jid > 0 THEN :first_jid ELSE matched_payment_jid END,
              matched_at = CASE WHEN :first_jid > 0 AND matched_at IS NULL THEN CURRENT_TIMESTAMP ELSE matched_at END,
              match_amount=:cp
          WHERE id=:id
        """), {
            "cp": cumulative_paid, "ub": unpaid, "jids": jids,
            "rate": annual_rate, "ei": estimated_interest,
            "warn": warning, "st": status, "id": cid,
            "first_jid": int(rows[0][0]) if rows else 0,
        })
        line = f"  {symbol} {name[:22]:<22} stmt={sd} due={due_d} ${total:>9,.2f} → {status:<16} {note}"
        print(line)
        if warning:
            print(f"      {warning}  est_interest=${estimated_interest:,.2f}")
    s.commit()
    print(f"\nSummary: {fully} fully paid, {partial} partial (interest), {under} underpaid, {overdue_n} overdue")


def show_status(s):
    """Print current status of all commitments — cumulative-payment view."""
    rows = s.execute(text("""
      SELECT card_coa, card_name, statement_date, total_due, minimum_due,
             payment_due_date, status, cumulative_paid, unpaid_balance,
             estimated_interest, interest_warning, payments_jids
      FROM cc_statement_commitment
      ORDER BY statement_date DESC, card_coa
    """)).all()
    print(f"\n=== CC Statement Commitments  ({len(rows)} total) ===\n")
    hdr = f"{'Card':<22} {'Stmt':<11} {'Due':<11} {'Total':>10} {'Paid':>10} {'Unpaid':>10} {'Status':<16}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        coa, name, sd, total, min_due, due, status, paid, unpaid, interest, warn, jids = r
        amt_s = f"${total:,.2f}" if total else "       ?"
        paid_s = f"${(paid or 0):,.2f}"
        unpaid_s = f"${(unpaid or 0):,.2f}"
        print(f"{name[:20]:<22} {str(sd)[:10]:<11} {str(due or '?')[:10]:<11} {amt_s:>10} {paid_s:>10} {unpaid_s:>10} {status:<16}")
        if warn:
            print(f"  └─ {warn}  est_interest≈${(interest or 0):,.2f}")
    print()
    print("=== Summary ===")
    r = s.execute(text("""
      SELECT status, COUNT(*), SUM(total_due), SUM(unpaid_balance), SUM(estimated_interest)
      FROM cc_statement_commitment GROUP BY status ORDER BY status
    """)).all()
    for x in r:
        n, total_due, unpaid, interest = x[1], float(x[2] or 0), float(x[3] or 0), float(x[4] or 0)
        extra = ""
        if unpaid > 0.01: extra = f"  unpaid=${unpaid:,.2f}  interest≈${interest:,.2f}"
        print(f"  {x[0]:<18}  {n:>3} stmts  total_due=${total_due:>12,.2f}{extra}")
    # Interest-bearing alert
    r = s.execute(text("""
      SELECT COUNT(*), SUM(unpaid_balance), SUM(estimated_interest)
      FROM cc_statement_commitment
      WHERE status IN ('matched_partial', 'underpaid', 'overdue')
        AND unpaid_balance > 0.01
    """)).fetchone()
    if r and r[0] > 0:
        print(f"\n⚠ INTEREST EXPOSURE: {r[0]} statements with unpaid balance, "
              f"${float(r[1] or 0):,.2f} total unpaid, ~${float(r[2] or 0):,.2f} interest accrued")
    # Upcoming
    today = date.today()
    soon = (today + timedelta(days=14)).isoformat()
    r = s.execute(text("""
      SELECT COUNT(*), SUM(total_due)
      FROM cc_statement_commitment
      WHERE status IN ('upcoming', 'pending') AND payment_due_date BETWEEN :t AND :s
    """), {"t": today.isoformat(), "s": soon}).fetchone()
    if r and r[0] > 0:
        print(f"\n⚠ UPCOMING (next 14 days): {r[0]} CC bills, ${float(r[1] or 0):,.2f} due")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="Scan CC PDFs and persist commitments")
    ap.add_argument("--match", action="store_true", help="Match pending commitments against POSB journals")
    ap.add_argument("--status", action="store_true", help="Show current commitment status")
    ap.add_argument("--all", action="store_true", help="Run scan + match + status")
    args = ap.parse_args()

    db.init_db()
    s = db.SessionLocal()
    try:
        ensure_table(s)
        if args.all or args.scan:
            scan_statements(s)
        if args.all or args.match:
            run_matcher(s)
        if args.all or args.status:
            show_status(s)
    finally:
        s.close()


if __name__ == "__main__":
    main()
