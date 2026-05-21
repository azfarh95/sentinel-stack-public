"""Opening balance manifest for any cutoff date — typically year-start.

For a given CUTOFF date (e.g. 2026-01-01), produces a per-CoA opening balance manifest:

  LIABILITY side:
    Read statement for month containing cutoff (or month-after for prev_balance), e.g.
    Jan'26 statement.previous_balance == balance on 2026-01-01.
    Fallback: prior-month closing_balance (= same value via chain).
    SC has split CC + BT openings via extras["previous_balance_by_coa"].
    Term-loan facilities (EZ Loan, Lending Bee, Sands): use PaymentSchedule —
    sum remaining principal of instalments due AFTER cutoff. Pre-origination = $0.

  ASSET side:
    Firefly current_balance MINUS sum of transactions on/after cutoff.
    Y2K38 cap: tx query end-date = today (Firefly stores Unix-time-32-bit; >2038 fails).
    USD accounts converted via balance_sheet_config.yaml usd_to_sgd.

Outputs:
    Console table + CSV at /data/opening_balance_<cutoff>.csv.
    With --post: writes balanced opening-balance journal to GL with
    counter-leg to 3210 Retained Earnings (Pre-cutoff).

Run:
    docker exec portfolio-mcp python -m app.opening_balance_extract --cutoff 2026-01-01
    docker exec portfolio-mcp python -m app.opening_balance_extract --cutoff 2026-01-01 --post
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import yaml
from sqlalchemy import select

from . import cc_statement_parser as p
from . import database as db
from . import journal_service as js

logger = logging.getLogger(__name__)

CC_STATEMENT_ROOT = Path("/onedrive/Sentinel Finance/02_Credit card statements")
FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")

CoA_NAMES = {
    # Assets
    "1111": "POSB Savings", "1112": "Cash Wallet", "1113": "Wise",
    "1114": "Maybank Savings", "1115": "SC Savings", "1116": "GXS Savings",
    "1211": "CPF OA", "1212": "CPF SA", "1213": "CPF MA",
    "1214": "CPF IS (parent)",
    "12141": "CPF IS — Franklin US Opps", "12142": "CPF IS — Allianz GHP",
    "12143": "CPF IS — Amova Japan Div", "12144": "CPF IS — Amova SG Eq",
    "12145": "CPF IS — abrdn SG Eq", "12149": "CPF IS — Unallocated",
    "1221": "ILP Tokio (parent)",
    "12211": "Tokio — Franklin Tech", "12212": "Tokio — Guinness Innov",
    "12213": "Tokio — Infinity US500", "12214": "Tokio — Canaccord Opp",
    "12215": "Tokio — FSSA India", "12219": "Tokio — Unallocated",
    "1222": "ILP Singlife (parent)",
    "12221": "Singlife — Allianz I&G", "12222": "Singlife — BGF Healthsci",
    "12223": "Singlife — Infinity US500", "12229": "Singlife — Unallocated",
    "1231": "Crypto",
    # Liabilities
    "2111": "DBS CC", "2112": "Maybank CC", "2113": "SC CC", "2114": "HSBC CC",
    "2121": "DBS Cashline", "2122": "UOB CashPlus",
    "2211": "SC Loan/BT", "2212": "GXS FlexiLoan", "2213": "Maybank CreditAble",
    "2221": "EZ Loan", "2222": "Lending Bee", "2223": "Sands Credit",
    # Equity
    "3210": "Retained Earnings (Pre-cutoff)",
}

# Firefly asset_id → CoA (assets only)
FIREFLY_ASSET_TO_COA = {
    1: "1111", 4: "1112", 168: "1113", 171: "1114", 172: "1115",
    141: "1211", 143: "1212", 145: "1213",
    147: "12149",                                 # CPF IS → Unallocated (1214 is header)
    162: "12219", 163: "12229",                   # ILPs → Unallocated leaves
    95: "1231", 97: "1231", 98: "1231",
}


# ── Liability side ──────────────────────────────────────────────────────────────

def _month_folder(d: date) -> Path:
    """Map a date to its CC_Statement folder. Format: 2026 = root/Mon'26, else root/YYYY/Mon'YY."""
    months = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",7:"July",
              8:"Aug",9:"Sept",10:"Oct",11:"Nov",12:"Dec"}
    folder = f"{months[d.month]}'{d.strftime('%y')}"
    if d.year == 2026:
        return CC_STATEMENT_ROOT / folder
    return CC_STATEMENT_ROOT / str(d.year) / folder


def extract_liability_openings(cutoff: date) -> dict[str, dict]:
    """Returns {coa: {amount, source, source_file}} for each liability facility.

    Strategy per CC statement:
      - Look in cutoff month's folder; use stmt.previous_balance
      - If no statement OR previous_balance is None, fall back to prior-month closing.
    """
    out: dict[str, dict] = {}
    # Primary source: month containing cutoff (e.g. Jan'26 statement for cutoff 2026-01-01)
    primary = _month_folder(cutoff)
    prior = _month_folder(cutoff - timedelta(days=1))

    primary_files = list(primary.glob("*.pdf")) if primary.exists() else []
    for pdf in primary_files:
        if "_Temp_" in pdf.name:
            continue
        try:
            s = p.detect_and_parse(str(pdf))
        except Exception as e:
            logger.warning("parse error %s: %s", pdf.name, e)
            continue
        if not s:
            continue
        # SC has per-CoA previous balances
        if s.extras.get("previous_balance_by_coa"):
            for coa, amt in s.extras["previous_balance_by_coa"].items():
                if amt is not None and coa.startswith("2"):  # liabilities only
                    out[coa] = {"amount": amt, "source": "stmt prev_balance",
                                "source_file": pdf.name}
        elif s.previous_balance is not None and s.facility_coa_code.startswith("2"):
            out[s.facility_coa_code] = {"amount": s.previous_balance,
                                        "source": "stmt prev_balance",
                                        "source_file": pdf.name}

    # Fallback: any CoA still missing — try prior-month closing
    if prior.exists():
        for pdf in prior.glob("*.pdf"):
            if "_Temp_" in pdf.name:
                continue
            try:
                s = p.detect_and_parse(str(pdf))
            except Exception:
                continue
            if not s:
                continue
            if s.extras.get("closing_balance_by_coa"):
                for coa, amt in s.extras["closing_balance_by_coa"].items():
                    if coa not in out and amt is not None and coa.startswith("2"):
                        out[coa] = {"amount": amt, "source": "prior-month close",
                                    "source_file": pdf.name}
            elif (s.closing_balance is not None
                  and s.facility_coa_code not in out
                  and s.facility_coa_code.startswith("2")):
                out[s.facility_coa_code] = {"amount": s.closing_balance,
                                            "source": "prior-month close",
                                            "source_file": pdf.name}
    return out


def extract_loan_openings(cutoff: date) -> dict[str, dict]:
    """For moneylenders + term loans: derive opening from PaymentSchedule.

    A loan originated AFTER cutoff has opening = 0.
    Otherwise: sum principal_portion of instalments due AFTER cutoff
    (these are the still-unpaid principal).
    """
    out: dict[str, dict] = {}
    coa_map = {"ez-loan": "2221", "lending-bee": "2222", "sands-credit": "2223"}

    s = db.SessionLocal()
    try:
        for fid, coa in coa_map.items():
            fac = s.get(db.CreditFacility, fid)
            if fac is None:
                continue
            cutoff_dt = datetime(cutoff.year, cutoff.month, cutoff.day)
            if fac.origination_date and fac.origination_date >= cutoff_dt:
                out[coa] = {"amount": 0.0, "source": f"originated {fac.origination_date.date()} (post-cutoff)",
                            "source_file": fac.id}
                continue
            sch = s.execute(
                select(db.PaymentSchedule)
                .where(db.PaymentSchedule.facility_id == fid)
                .order_by(db.PaymentSchedule.due_date)
            ).scalars().all()
            remaining = sum(ps.principal_portion or 0
                            for ps in sch if ps.due_date > cutoff_dt)
            out[coa] = {"amount": remaining, "source": "PaymentSchedule remaining P",
                        "source_file": fac.id}
    finally:
        s.close()
    return out


# ── Asset side ──────────────────────────────────────────────────────────────────

def _load_fx() -> float:
    try:
        cfg = yaml.safe_load(open("/finance/balance_sheet_config.yaml"))
        return float(cfg.get("usd_to_sgd", 1.27))
    except Exception:
        return 1.27


def extract_asset_openings(cutoff: date) -> dict[str, dict]:
    """For each Firefly asset account, compute balance on (cutoff - 1 day):
       balance(cutoff-1) = current_balance - sum(tx amounts from cutoff onwards)
    Y2K38-safe: clamps end date to today.
    Returns {coa: {amount_sgd, source, source_file}}. CoAs may be aggregated
    (e.g. multiple crypto wallets all roll to 1231).
    """
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        logger.warning("FIREFLY_PAT missing; asset side will be empty")
        return {}

    fx = _load_fx()
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    start = cutoff.isoformat()
    end = date.today().isoformat()

    aggregated: dict[str, dict] = defaultdict(lambda: {"amount": 0.0, "source": "Firefly", "source_file": []})
    with httpx.Client(timeout=60) as c:
        for aid, coa in FIREFLY_ASSET_TO_COA.items():
            try:
                r = c.get(f"{FIREFLY_URL}/api/v1/accounts/{aid}", headers=headers)
                r.raise_for_status()
                attr = r.json()["data"]["attributes"]
                live = float(attr.get("current_balance") or 0)
                cur = attr.get("currency_code") or "SGD"
                name = attr.get("name", f"acct{aid}")
            except Exception as e:
                logger.warning("firefly account %s fetch failed: %s", aid, e)
                continue
            # Sum tx delta from cutoff onwards
            delta = 0.0
            page = 1
            while True:
                try:
                    r = c.get(f"{FIREFLY_URL}/api/v1/accounts/{aid}/transactions",
                              params={"start": start, "end": end, "limit": 200, "page": page},
                              headers=headers)
                    r.raise_for_status()
                except Exception as e:
                    logger.warning("firefly tx %s fetch failed: %s", aid, e); break
                d = r.json()
                rows = d.get("data", [])
                if not rows:
                    break
                for tx in rows:
                    for split in tx["attributes"]["transactions"]:
                        amt = float(split["amount"])
                        src = int(split["source_id"])
                        dst = int(split["destination_id"])
                        sign = (1 if dst == aid else 0) - (1 if src == aid else 0)
                        delta += sign * amt
                meta = d.get("meta", {}).get("pagination", {})
                if page >= int(meta.get("total_pages", 1)):
                    break
                page += 1
            opening = live - delta
            opening_sgd = opening * (fx if cur == "USD" else 1.0)
            aggregated[coa]["amount"] += opening_sgd
            aggregated[coa]["source_file"].append(f"firefly_acct {aid} ({name})")
    # Flatten source_file list to string
    for coa in aggregated:
        aggregated[coa]["source_file"] = "; ".join(aggregated[coa]["source_file"])
    return dict(aggregated)


# ── Orchestrator ────────────────────────────────────────────────────────────────

def build_manifest(cutoff: date) -> tuple[dict, dict]:
    """Returns (liabilities_dict, assets_dict). Each: {coa: {amount, source, source_file}}."""
    liabs = extract_liability_openings(cutoff)
    liabs.update(extract_loan_openings(cutoff))
    assets = extract_asset_openings(cutoff)
    return liabs, assets


def print_manifest(cutoff: date, liabs: dict, assets: dict) -> None:
    print(f"\n=== Opening balance manifest @ {cutoff.isoformat()} ===\n")

    print("ASSETS (DR)")
    print(f"  {'CoA':<6} {'Name':<24} {'Amount':>13} {'Source':<22} {'Reference'}")
    print("  " + "-" * 92)
    total_a = 0.0
    for coa in sorted(assets):
        r = assets[coa]
        total_a += r["amount"]
        print(f"  {coa:<6} {CoA_NAMES.get(coa, '?'):<24} {r['amount']:>13,.2f}  "
              f"{r['source']:<22} {r['source_file'][:50]}")
    print("  " + "-" * 92)
    print(f"  {'TOTAL ASSETS':<31} {total_a:>13,.2f}")

    print("\nLIABILITIES (CR)")
    print(f"  {'CoA':<6} {'Name':<24} {'Amount':>13} {'Source':<22} {'Reference'}")
    print("  " + "-" * 92)
    total_l = 0.0
    for coa in sorted(liabs):
        r = liabs[coa]
        total_l += r["amount"]
        print(f"  {coa:<6} {CoA_NAMES.get(coa, '?'):<24} {r['amount']:>13,.2f}  "
              f"{r['source']:<22} {r['source_file'][:50]}")
    print("  " + "-" * 92)
    print(f"  {'TOTAL LIABILITIES':<31} {total_l:>13,.2f}")

    net_equity = total_a - total_l
    print(f"\n  NET (Retained Earnings 3210): SGD {net_equity:,.2f}")


def write_csv(cutoff: date, liabs: dict, assets: dict, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["cutoff", "side", "coa", "name", "amount", "source", "source_file"])
        for coa, r in sorted(assets.items()):
            w.writerow([cutoff.isoformat(), "ASSET", coa, CoA_NAMES.get(coa, "?"),
                        f"{r['amount']:.2f}", r["source"], r["source_file"]])
        for coa, r in sorted(liabs.items()):
            w.writerow([cutoff.isoformat(), "LIABILITY", coa, CoA_NAMES.get(coa, "?"),
                        f"{r['amount']:.2f}", r["source"], r["source_file"]])
    print(f"\n  CSV written: {path}")


def post_opening_journal(cutoff: date, liabs: dict, assets: dict) -> int:
    """Post balanced opening-balance journal: DR assets, CR liabilities, plug to 3210."""
    lines = []
    total_a = round(sum(r["amount"] for r in assets.values()), 2)
    total_l = round(sum(r["amount"] for r in liabs.values()), 2)
    for coa, r in assets.items():
        amt = round(r["amount"], 2)
        if abs(amt) < 0.005:        # skip near-zero (float rounding noise)
            continue
        if amt < 0:                  # negative asset opening = treat as credit
            lines.append({"account_code": coa, "credit": -amt,
                          "narration": f"Opening balance @ {cutoff.isoformat()} ({r['source']})"})
        else:
            lines.append({"account_code": coa, "debit": amt,
                          "narration": f"Opening balance @ {cutoff.isoformat()} ({r['source']})"})
    for coa, r in liabs.items():
        amt = round(r["amount"], 2)
        if abs(amt) < 0.005:
            continue
        if amt < 0:                  # negative liability = treat as debit
            lines.append({"account_code": coa, "debit": -amt,
                          "narration": f"Opening balance @ {cutoff.isoformat()} ({r['source']})"})
        else:
            lines.append({"account_code": coa, "credit": amt,
                          "narration": f"Opening balance @ {cutoff.isoformat()} ({r['source']})"})
    # Plug equity (use leaf code under 3000 — 3210 doesn't exist; use 3100 Retained Earnings prior)
    net = round(total_a - total_l, 2)
    equity_coa = "3100"
    if abs(net) > 0.005:
        if net > 0:
            lines.append({"account_code": equity_coa, "credit": net,
                          "narration": f"Retained earnings plug @ {cutoff.isoformat()}"})
        else:
            lines.append({"account_code": equity_coa, "debit": -net,
                          "narration": f"Retained earnings plug @ {cutoff.isoformat()}"})
    s = db.SessionLocal()
    try:
        jid = js.post_journal(
            s,
            journal_date=cutoff,
            narration=f"OPENING BALANCE @ {cutoff.isoformat()}",
            journal_type="opening",
            lines=lines,
            source_doc="OPENING_BALANCE",
            source_ref=cutoff.isoformat(),
            external_id=f"opening:{cutoff.isoformat()}",
        )
        s.commit()
        return jid
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-01-01",
                    help="Opening-balance date (YYYY-MM-DD). Default: 2026-01-01")
    ap.add_argument("--csv", help="Output CSV path. Default: /data/opening_balance_<cutoff>.csv")
    ap.add_argument("--post", action="store_true",
                    help="Post the opening-balance journal to GL")
    args = ap.parse_args()

    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d").date()
    liabs, assets = build_manifest(cutoff)
    print_manifest(cutoff, liabs, assets)

    csv_path = Path(args.csv) if args.csv else Path(f"/data/opening_balance_{cutoff.isoformat()}.csv")
    write_csv(cutoff, liabs, assets, csv_path)

    if args.post:
        jid = post_opening_journal(cutoff, liabs, assets)
        print(f"\n  Posted journal #{jid}")


if __name__ == "__main__":
    main()
