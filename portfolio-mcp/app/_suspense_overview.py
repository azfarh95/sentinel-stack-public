"""Anatomy of the 1,693 suspense items — what tx_types, what amounts, what carriers."""
from app import database as db
from sqlalchemy import text

db.init_db()
s = db.SessionLocal()

# Pull all suspense (1190/4900) legs from direct journals
rows = s.execute(text("""
    SELECT
        j.id, j.journal_date, j.narration,
        gl.debit, gl.credit, gl.narration as gl_narr,
        coa.account_code, j.source_doc
    FROM journals j
    JOIN general_ledger gl ON gl.journal_id = j.id
    JOIN chart_of_accounts coa ON coa.id = gl.account_id
    WHERE j.status = 'posted'
      AND j.source_doc LIKE '%_DIRECT%'
      AND coa.account_code IN ('1190', '4900')
""")).all()

print(f"Total suspense legs: {len(rows)}\n")

# 1. By tx_type prefix
from collections import Counter
tx_types = Counter()
for r in rows:
    # j.narration has format "[direct POSB] {tx_type} — {reason}"
    narr = (r[2] or "").replace("[direct POSB] ", "").replace("[direct CC POSB Savings", "")
    # Extract tx_type (everything before " — ")
    if " — " in narr:
        tx_type = narr.split(" — ")[0].strip()
    else:
        tx_type = narr[:40]
    # Normalize: collapse trailing reference numbers / dates
    parts = tx_type.split()
    if len(parts) > 4: tx_type = " ".join(parts[:4])
    tx_types[tx_type] += 1

print("=== Top 20 suspense tx_type patterns ===")
for tx_type, count in tx_types.most_common(20):
    print(f"  {count:>5}  {tx_type[:70]}")

# 2. Amount distribution
print("\n=== Amount distribution of suspense items ===")
amounts = [float(r[3] or 0) + float(r[4] or 0) for r in rows]
amounts.sort()
n = len(amounts)
print(f"  n={n}  total=${sum(amounts):,.2f}")
print(f"  min=${amounts[0]:,.2f}  median=${amounts[n//2]:,.2f}  max=${amounts[-1]:,.2f}")
buckets = {"<$10": 0, "$10-50": 0, "$50-200": 0, "$200-1000": 0, ">$1000": 0}
sums =    {"<$10": 0.0, "$10-50": 0.0, "$50-200": 0.0, "$200-1000": 0.0, ">$1000": 0.0}
for a in amounts:
    if a < 10:       buckets["<$10"] += 1; sums["<$10"] += a
    elif a < 50:     buckets["$10-50"] += 1; sums["$10-50"] += a
    elif a < 200:    buckets["$50-200"] += 1; sums["$50-200"] += a
    elif a < 1000:   buckets["$200-1000"] += 1; sums["$200-1000"] += a
    else:            buckets[">$1000"] += 1; sums[">$1000"] += a
print(f"\n  {'Bucket':<14} {'Count':>6} {'%':>6}  {'Total $':>14}  {'% $':>6}")
for b in ["<$10", "$10-50", "$50-200", "$200-1000", ">$1000"]:
    pct_n = 100 * buckets[b] / n
    pct_v = 100 * sums[b] / sum(amounts)
    print(f"  {b:<14} {buckets[b]:>6} {pct_n:>5.1f}%  ${sums[b]:>12,.2f}  {pct_v:>5.1f}%")

# 3. By carrier presence — look at gl_narr for clues
print("\n=== Suspense by carrier signal (top patterns in narration) ===")
markers = Counter()
for r in rows:
    gl_narr = (r[5] or "").upper()
    if "PAYNOW" in gl_narr: markers["PAYNOW"] += 1
    elif "MEPS" in gl_narr: markers["MEPS Receipt"] += 1
    elif "DEBIT CARD" in gl_narr or "POS" in gl_narr: markers["DEBIT CARD"] += 1
    elif "INTERNET BANKING" in gl_narr or "IBT" in gl_narr: markers["INTERNET BANKING TRF"] += 1
    elif "FAST" in gl_narr: markers["FAST"] += 1
    elif "GIRO" in gl_narr: markers["GIRO"] += 1
    elif "ATM" in gl_narr or "CASH WITH" in gl_narr: markers["ATM/CASH"] += 1
    elif "INTEREST" in gl_narr: markers["INTEREST"] += 1
    elif "FEE" in gl_narr: markers["FEE"] += 1
    elif "SALARY" in gl_narr: markers["SALARY"] += 1
    elif "BILL" in gl_narr: markers["BILL"] += 1
    elif "DEPOSIT" in gl_narr: markers["DEPOSIT"] += 1
    elif "INCOMING" in gl_narr: markers["INCOMING TRANSFER"] += 1
    elif "OUTGOING" in gl_narr: markers["OUTGOING TRANSFER"] += 1
    elif "TRANSFER" in gl_narr: markers["TRANSFER (other)"] += 1
    else: markers["NO CARRIER"] += 1
for k, v in markers.most_common():
    print(f"  {v:>5}  {k}")

# 4. By year
print("\n=== Suspense by year ===")
year_counts = Counter()
year_sums = {}
for r in rows:
    yr = str(r[1])[:4]
    year_counts[yr] += 1
    year_sums[yr] = year_sums.get(yr, 0.0) + float(r[3] or 0) + float(r[4] or 0)
for yr in sorted(year_counts):
    print(f"  {yr}  n={year_counts[yr]:>5}  $={year_sums[yr]:>14,.2f}")

# 5. Sample of biggest suspense items — likely high-value misses
print("\n=== Top 15 biggest suspense items (likely high-value misses) ===")
rows_sorted = sorted(rows, key=lambda r: -(float(r[3] or 0) + float(r[4] or 0)))
for r in rows_sorted[:15]:
    amt = float(r[3] or 0) + float(r[4] or 0)
    print(f"  ${amt:>10,.2f}  {str(r[1])[:10]}  {(r[5] or '')[:100]}")

s.close()
