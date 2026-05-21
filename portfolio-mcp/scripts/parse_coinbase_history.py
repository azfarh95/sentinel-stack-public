"""Parse the tab-delimited Coinbase history paste into a sorted CSV + summary.

One-shot transformation, not a long-lived module — saved under scripts/ so it
doesn't accumulate under app/.
"""
import csv
import re
from datetime import datetime
from collections import defaultdict
from pathlib import Path

SRCS = [Path("/data/coinbase_history_raw.txt"),
        Path("/data/coinbase_history_raw_batch2.txt")]
DST = Path("/data/coinbase_history_sorted.csv")

rows = []
seen = set()  # dedupe key
for src in SRCS:
    if not src.exists():
        continue
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        dt_str, activity, desc, amt_native, amt_usd = parts[0], parts[1], parts[2], parts[3], parts[4]
        status = parts[5] if len(parts) > 5 else ""
        dt = datetime.strptime(dt_str, "%b %d, %Y, %H:%M:%S")
        m = re.match(r"([\-\+]?[\d,]+(?:\.\d+)?)\s+([A-Z]+)", amt_native.strip())
        if m:
            amt_val = float(m.group(1).replace(",", ""))
            asset = m.group(2)
        else:
            amt_val, asset = 0.0, ""
        m = re.match(r"([\-\+]?)\$([\d,]+(?:\.\d+)?)", amt_usd.strip())
        usd = float(m.group(2).replace(",", "")) * (-1 if m.group(1) == "-" else 1) if m else 0.0
        key = (dt.isoformat(sep=" "), activity, round(amt_val, 8), asset, round(usd, 4))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "datetime": dt.isoformat(sep=" "),
            "activity": activity,
            "description": desc,
            "amount": amt_val,
            "asset": asset,
            "usd": usd,
            "status": status,
        })

# Sort chronologically (oldest first)
rows.sort(key=lambda r: r["datetime"])

# Write CSV
with DST.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["datetime","activity","description","amount","asset","usd","status"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
print(f"wrote {len(rows)} rows to {DST}")

# Summary by activity
print("\n=== By Activity (USD total) ===")
agg = defaultdict(lambda: {"n": 0, "usd": 0.0, "native_sum": 0.0})
for r in rows:
    k = r["activity"]
    agg[k]["n"] += 1
    agg[k]["usd"] += r["usd"]
    agg[k]["native_sum"] += r["amount"]
print(f"{'Activity':<22} {'N':>4} {'USD total':>15} {'Native sum':>20}")
print("-" * 65)
for k in sorted(agg, key=lambda x: -abs(agg[x]['usd'])):
    v = agg[k]
    print(f"{k:<22} {v['n']:>4} {v['usd']:>15,.2f} {v['native_sum']:>20,.4f}")

# Net SGD flow
sgd_in = sum(r["amount"] for r in rows if r["asset"] == "SGD" and r["amount"] > 0)
sgd_out = abs(sum(r["amount"] for r in rows if r["asset"] == "SGD" and r["amount"] < 0))
print(f"\n=== SGD flow (POSB ↔ Coinbase) ===")
print(f"  Deposited from POSB: SGD {sgd_in:>12,.2f}")
print(f"  Withdrew to POSB:    SGD {sgd_out:>12,.2f}")
print(f"  Net (in - out):      SGD {sgd_in - sgd_out:>12,.2f}")

# USDC flow with wallet (Sent + Received)
sent_usdc = sum(abs(r["amount"]) for r in rows if r["activity"] == "Sent USDC")
recv_usdc = sum(r["amount"] for r in rows if r["activity"] == "Received USDC")
print(f"\n=== USDC flow (Coinbase ↔ DeFi wallet 0xd87...d751) ===")
print(f"  Sent OUT to wallet:    USDC {sent_usdc:>12,.4f}  (~ ${sent_usdc:,.2f})")
print(f"  Received FROM wallet:  USDC {recv_usdc:>12,.4f}  (~ ${recv_usdc:,.2f})")
print(f"  Net (out - in):        USDC {sent_usdc - recv_usdc:>12,.4f}")
