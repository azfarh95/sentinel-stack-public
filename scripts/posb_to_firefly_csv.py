"""
POSB bank statement (PDF) → Firefly III CSV transformer.
v2 — uses pdfplumber to read x-coordinates so we can distinguish
WITHDRAWAL vs DEPOSIT columns definitively.

POSB layout (consistent across 2024-2026 statements):
  WITHDRAWAL($) column: x≈313-365
  DEPOSIT($)    column: x≈408-440
  BALANCE($)    column: x≈484-540

Approach:
  1. Walk pages; for each page, find amount-shaped text and read its x0.
  2. If x0 < 380  → withdrawal (outgoing, negative amount)
  3. If 380 ≤ x0 < 460  → deposit (incoming, positive amount)
  4. If x0 ≥ 460 → running balance (skip, used only for validation)
  5. Pair each amount with its preceding date + descriptor lines.

Output: per-year CSV ready for Firefly III Data Importer.
"""

import re, os, csv, glob
from datetime import datetime
import pdfplumber

PDF_DIR = r"C:\Users\azfar\OneDrive\CC_Statement\Statements by bank\Bank Statements"
OUT_DIR = r"C:\Users\azfar\OneDrive\CC_Statement\firefly_csv"
ASSET_ACCOUNT = "POSB Savings"

# Column boundaries (verified empirically on Jan 2026 statement)
X_WITHDRAWAL_MAX = 380.0
X_DEPOSIT_MAX    = 460.0
# x ≥ 460 → balance

# Known insurance amounts → policy
INSURANCE_MAP = {
    "11.51":  ("Singlife Cancer Cover Plus II", "Insurance"),
    "11.09":  ("Singlife Cancer Cover Plus II", "Insurance"),
    "69.01":  ("Singlife Health Plus",          "Insurance"),
    "96.55":  ("Singlife Whole Life",           "Insurance"),
    "252.85": ("Singlife Savvy Invest",         "Insurance"),
    "83.70":  ("Singlife Multipay CI",          "Insurance"),
    "39.60":  ("Singlife Mindef GTL",           "Insurance"),
    "39.51":  ("Singlife Mindef GTL",           "Insurance"),
    "39.15":  ("Singlife Mindef GTL",           "Insurance"),
    "3.05":   ("Singlife (small rider)",        "Insurance"),
    "418.45": ("Tokio Marine Wealth Pro",       "Insurance"),
}

MONTH_NUM = {m: i+1 for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
)}
DATE_RE   = re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b")
AMT_RE    = re.compile(r"^[\d,]+\.\d{2}$")


def parse_meta(fn):
    m = re.search(r"_(\w{3})(\d{4})\.pdf$", fn)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def to_float(s): return float(s.replace(",", ""))


def extract_lines_with_x(page):
    """Return list of (text, x0_of_first_char) tuples — one per visual line."""
    # pdfplumber's extract_words groups characters into words with bboxes
    words = page.extract_words(keep_blank_chars=False, use_text_flow=True)
    # Group words into lines by 'top' (y-coordinate, with tolerance)
    lines = {}
    for w in words:
        top = round(w['top'] / 2) * 2  # bucket lines within 2pt
        lines.setdefault(top, []).append(w)
    out = []
    for top in sorted(lines.keys()):
        ws = sorted(lines[top], key=lambda w: w['x0'])
        line_text = ' '.join(w['text'] for w in ws)
        # For each word that LOOKS like an amount, record its x0
        amount_words = [(w['text'], w['x0']) for w in ws if AMT_RE.match(w['text'])]
        out.append({'text': line_text, 'words': ws, 'amounts': amount_words})
    return out


def parse_statement(pdf_path):
    """Yield transaction dicts with definitive direction."""
    mon, year = parse_meta(os.path.basename(pdf_path))
    if not year:
        return

    current_date = None
    descriptor_buf = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for line in extract_lines_with_x(page):
                text = line['text'].strip()
                # Skip headers/footers/page breaks
                if not text: continue
                if 'BALANCE($)' in text.upper(): continue
                if text.startswith('Page '): continue
                if 'PDS_POSB' in text: continue
                if 'Balance Brought Forward' in text or 'Balance Carried Forward' in text:
                    descriptor_buf = []
                    continue
                # Statement summary row: "Total <withdrawals> <deposits>" with no
                # descriptor. Found at end of statement; was being parsed as a
                # phantom month-end transaction with description "Unknown".
                if re.match(r'^Total\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}\s*$', text):
                    descriptor_buf = []
                    continue

                # Date line — POSB puts date + descriptor + amount on the
                # SAME visual row (different columns), so we must process
                # BOTH the date prefix and any amount on the same line.
                dm = DATE_RE.match(text)
                date_just_set = False
                if dm:
                    descriptor_buf = []
                    try:
                        current_date = datetime(year, MONTH_NUM[dm.group(2)], int(dm.group(1)))
                        date_just_set = True
                    except ValueError:
                        current_date = None
                    # Take the textual part of the line BETWEEN the date and
                    # before any amount as the descriptor seed.
                    rest = text[dm.end():].strip()
                    # Strip trailing amount tokens from the descriptor seed
                    rest = re.sub(r'\s+[\d,]+\.\d{2}(\s+[\d,]+\.\d{2})?\s*$', '', rest).strip()
                    if rest:
                        descriptor_buf.append(rest)
                    # FALL THROUGH to amount detection below — don't `continue` yet.

                # Check if this line contains transaction amount(s) — both
                # standalone amount lines AND amount-on-same-line-as-date.
                if line['amounts'] and current_date:
                    txn_amt = None
                    direction = None
                    for amt_text, x in line['amounts']:
                        if x < X_WITHDRAWAL_MAX:
                            txn_amt = amt_text; direction = 'out'; break
                        elif x < X_DEPOSIT_MAX:
                            txn_amt = amt_text; direction = 'in'; break
                        # else: balance — skip and keep scanning
                    if txn_amt:
                        yield {
                            'date': current_date.strftime("%Y-%m-%d"),
                            'amount': to_float(txn_amt),
                            'amount_str': txn_amt,
                            'direction': direction,
                            'descriptor_lines': list(descriptor_buf),
                        }
                        descriptor_buf = []
                        continue

                # Otherwise: descriptor line. Skip if it was a pure date line
                # (no amount, already buffered above).
                if date_just_set:
                    continue
                descriptor_buf.append(text)


def extract_counterparty(descriptor_lines):
    for line in descriptor_lines:
        m = re.search(r"^(?:To|From):\s*(.+?)\s*-?\s*$", line)
        if m:
            return m.group(1).strip()
        if re.match(r"^[A-Z][A-Z\s&,.()-]{8,}$", line.strip()):
            return line.strip()
    for line in descriptor_lines:
        if line.strip() and not re.match(
            r"^(FAST Payment|Payments / Collections|Debit Card transaction|Bill Payment|Interest Earned|PayNow Transfer|Other|Transfer)",
            line.strip()
        ):
            return line.strip()[:80]
    return ""


def to_firefly_row(tx):
    descriptor = tx['descriptor_lines']
    direction = tx['direction']
    counterparty = extract_counterparty(descriptor) or "Unknown"
    description = (descriptor[0] if descriptor else counterparty)[:100]
    if counterparty and counterparty != description:
        description = f"{description} - {counterparty}"[:140]

    amount = tx['amount'] if direction == 'in' else -tx['amount']

    category = ""
    tags = ""
    amt_key = tx['amount_str'].replace(",", "")
    if amt_key in INSURANCE_MAP and direction == 'out':
        policy, cat = INSURANCE_MAP[amt_key]
        category = cat
        tags = policy

    if direction == 'out':
        src, dst = ASSET_ACCOUNT, counterparty
    else:
        src, dst = counterparty, ASSET_ACCOUNT

    return {
        "date": tx['date'],
        "amount": f"{amount:.2f}",
        "description": description,
        "source_name": src,
        "destination_name": dst,
        "category": category,
        "tags": tags,
        "notes": " | ".join(descriptor)[:500],
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    all_rows = []
    pdfs = sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf")))
    print(f"Parsing {len(pdfs)} statements with column-aware extraction...")
    for pdf in pdfs:
        txns = list(parse_statement(pdf))
        rows = [to_firefly_row(t) for t in txns]
        all_rows.extend(rows)
        n_in = sum(1 for r in rows if float(r['amount']) > 0)
        n_out = len(rows) - n_in
        print(f"  {os.path.basename(pdf)}: {len(rows)} txns ({n_in} in / {n_out} out)")

    all_rows.sort(key=lambda r: r["date"])

    cols = ["date","amount","description","source_name","destination_name","category","tags","notes"]
    per_year = {}
    for r in all_rows:
        per_year.setdefault(r["date"][:4], []).append(r)

    for y, rows in sorted(per_year.items()):
        out = os.path.join(OUT_DIR, f"posb_{y}.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        in_sum = sum(float(r['amount']) for r in rows if float(r['amount']) > 0)
        out_sum = -sum(float(r['amount']) for r in rows if float(r['amount']) < 0)
        print(f"  Wrote {len(rows):4d} rows -> {os.path.basename(out)}  in=SGD{in_sum:>10.2f}  out=SGD{out_sum:>10.2f}  net={in_sum-out_sum:>+10.2f}")

    print()
    from collections import Counter
    cats = Counter(r["category"] or "(uncategorized)" for r in all_rows)
    print("Category breakdown:")
    for c, n in cats.most_common():
        print(f"  {n:5d}  {c}")
    print(f"\nTotal: {len(all_rows)} transactions")


if __name__ == "__main__":
    main()
