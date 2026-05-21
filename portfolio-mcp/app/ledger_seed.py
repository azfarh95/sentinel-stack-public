"""Seed Chart of Accounts + initial Party master from /finance YAML.

Run inside the portfolio-mcp container:
    docker exec portfolio-mcp python -m app.ledger_seed

Idempotent: each CoA node is upserted by account_code. Parties are upserted
by party_code.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import yaml

from . import database as db
from . import ledger

logger = logging.getLogger(__name__)


# Full IAS 1 chart of accounts for Sentinel Finance (personal-finance flavour).
# Each tuple: (code, name, parent_code, class, subclass, normal_balance, sub_ledger_table?)
# Header (non-postable) rows have no normal_balance constraint and is_postable=False.
COA_TREE = [
    # ── 1xxx ASSETS ────────────────────────────────────────────────────────
    ("1000", "ASSETS",                              None,   "ASSET",     None,             "DEBIT",  False, None),
    # Current Assets
    ("1100", "Current Assets",                      "1000", "ASSET",     "CURRENT_ASSET",  "DEBIT",  False, None),
    ("1110", "Cash & Equivalents",                  "1100", "ASSET",     "CURRENT_ASSET",  "DEBIT",  False, None),
    ("1111", "POSB Savings",                        "1110", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  None),
    ("1112", "Cash Wallet",                         "1110", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  None),
    ("1113", "Wise Multi-Currency",                 "1110", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  None),
    ("1114", "Maybank Savings (Ar Rihla)",          "1110", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  None),
    ("1115", "Standard Chartered Savings",          "1110", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  None),
    ("1120", "Receivables & Recoverables",          "1100", "ASSET",     "CURRENT_ASSET",  "DEBIT",  False, None),
    ("1121", "Tax Refund Receivable (IRAS)",        "1120", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  None),
    ("1122", "Unpaid CPF — Ganesan (recoverable)",  "1120", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  "receivables_ledger"),
    ("1123", "Employer CPF Receivable (general)",   "1120", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  "receivables_ledger"),
    ("1124", "Family loans-out (receivable)",       "1120", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  "receivables_ledger"),
    # Suspense — required forcing function for double-entry.
    # Any tx with ambiguous other-leg posts here until manually classified.
    ("1190", "Suspense Account",                    "1100", "ASSET",     "CURRENT_ASSET",  "DEBIT",  True,  None),
    # Non-Current Assets
    ("1200", "Non-Current Assets",                  "1000", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", False, None),
    ("1210", "Investments — CPF",                   "1200", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", False, None),
    ("1211", "CPF Ordinary Account",                "1210", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", True,  None),
    ("1212", "CPF Special Account",                 "1210", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", True,  None),
    ("1213", "CPF MediSave",                        "1210", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", True,  None),
    # 1214 converted to header (Pass A 2026-05-14) — children are per-fund 5-digit leaves.
    ("1214", "CPF Investment Scheme (CPFIS-OA)",    "1210", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", False, "investment_positions"),
    ("12141", "FTIF Franklin US Opportunities SGD (CPF)",  "1214", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12142", "Allianz Global High Payout AM SGD (CPF)",   "1214", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12143", "Amova Japan Dividend Equity SGD-H (CPF)",   "1214", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12144", "Amova Singapore Equity SGD (CPF)",          "1214", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12145", "abrdn Singapore Equity SGD (CPF)",          "1214", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12149", "CPF IS — Unallocated",                      "1214", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, None),
    ("1220", "Investments — Insurance-Linked",      "1200", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", False, None),
    # 1221 + 1222 converted to headers (Pass A) — children are per-fund 5-digit leaves.
    ("1221", "Tokio Marine ILP",                    "1220", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", False, "investment_positions"),
    ("12211", "Franklin Technology SGD-H (Tokio)",        "1221", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12212", "Guinness Global Innovators USD (Tokio)",   "1221", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12213", "Infinity US 500 SGD (Tokio)",              "1221", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12214", "Canaccord Genuity Opportunity SGD-H",      "1221", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12215", "FSSA Regional India SGD (Tokio)",          "1221", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12219", "Tokio Marine — Unallocated",               "1221", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, None),
    ("1222", "Singlife Savvy Invest ILP",           "1220", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", False, "investment_positions"),
    ("12221", "Allianz Inc & Growth AMH2 SGD (Singlife)", "1222", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12222", "BGF World Healthsci A2 SGD-H (Singlife)",  "1222", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12223", "Infinity US 500 SGD (Singlife)",           "1222", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, "investment_positions"),
    ("12229", "Singlife Savvy Invest — Unallocated",      "1222", "ASSET", "NON_CURRENT_ASSET", "DEBIT", True, None),
    ("1230", "Investments — Crypto",                "1200", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", False, None),
    ("1231", "Crypto Wallet (liquid)",              "1230", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", True,  "investment_positions"),
    ("1232", "WolfSwap PACK Stake (Cronos)",        "1230", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", True,  "investment_positions"),
    ("1233", "Krystal LP & Vault Positions",        "1230", "ASSET",     "NON_CURRENT_ASSET", "DEBIT", True,  "investment_positions"),

    # ── 2xxx LIABILITIES ───────────────────────────────────────────────────
    ("2000", "LIABILITIES",                         None,   "LIABILITY", None,                  "CREDIT", False, None),
    # Current Liabilities
    ("2100", "Current Liabilities",                 "2000", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", False, None),
    ("2110", "Credit Cards",                        "2100", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", False, None),
    ("2111", "DBS CC (4119-...-2424)",              "2110", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", True,  "credit_facilities"),
    ("2112", "Maybank CC (4966-...-7004)",          "2110", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", True,  "credit_facilities"),
    ("2113", "SC CC (5498-...-8810)",               "2110", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", True,  "credit_facilities"),
    ("2114", "HSBC CC (4835-...-5159)",             "2110", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", True,  "credit_facilities"),
    ("2115", "Atome (BNPL)",                        "2110", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", True,  None),
    ("2120", "Lines of Credit",                     "2100", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", False, None),
    ("2121", "DBS Cashline (085-043736-4)",         "2120", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", True,  "credit_facilities"),
    ("2122", "UOB CashPlus (465-349-508-5)",        "2120", "LIABILITY", "CURRENT_LIABILITY",   "CREDIT", True,  "credit_facilities"),
    # Non-Current Liabilities (term loans + moneylenders)
    ("2200", "Non-Current Liabilities",             "2000", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", False, None),
    ("2210", "Term Loans — Banks",                  "2200", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", False, None),
    ("2211", "SC Loan / BT (9702-...-6461)",        "2210", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", True,  "credit_facilities"),
    ("2212", "GXS FlexiLoan (800-170405-95)",       "2210", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", True,  "credit_facilities"),
    ("2213", "Maybank CreditAble (0413-...-707)",   "2210", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", True,  "credit_facilities"),
    ("2220", "Term Loans — Moneylenders",           "2200", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", False, None),
    ("2221", "EZ Loan (EL-14603)",                  "2220", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", True,  "credit_facilities"),
    ("2222", "Lending Bee",                         "2220", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", True,  "credit_facilities"),
    ("2223", "Sands Credit (16125/2025)",           "2220", "LIABILITY", "NON_CURRENT_LIABILITY", "CREDIT", True,  "credit_facilities"),

    # ── 3xxx EQUITY ────────────────────────────────────────────────────────
    ("3000", "EQUITY",                              None,   "EQUITY", None,                "CREDIT", False, None),
    ("3100", "Retained Earnings (prior periods)",   "3000", "EQUITY", "RETAINED_EARNINGS", "CREDIT", True,  None),
    ("3200", "Current Period P&L",                  "3000", "EQUITY", "CURRENT_PERIOD_PL", "CREDIT", True,  None),
    ("3300", "Unrealized Gains / Losses",           "3000", "EQUITY", "OCI",               "CREDIT", True,  None),

    # ── 4xxx REVENUE ───────────────────────────────────────────────────────
    ("4000", "REVENUE",                             None,   "REVENUE", None,              "CREDIT", False, None),
    ("4100", "Employment Income",                   "4000", "REVENUE", "OPERATING_REV",   "CREDIT", False, None),
    ("4110", "Salary — AZ United",                  "4100", "REVENUE", "OPERATING_REV",   "CREDIT", True,  None),
    ("4120", "Salary — YourAgency Security",         "4100", "REVENUE", "OPERATING_REV",   "CREDIT", True,  None),
    ("4130", "Reimbursement — SAF",                 "4100", "REVENUE", "OPERATING_REV",   "CREDIT", True,  None),
    ("4200", "Investment Income",                   "4000", "REVENUE", "INVESTMENT_REV",  "CREDIT", False, None),
    ("4210", "Dividend Income",                     "4200", "REVENUE", "INVESTMENT_REV",  "CREDIT", True,  None),
    ("4220", "Interest Income",                     "4200", "REVENUE", "INVESTMENT_REV",  "CREDIT", True,  None),
    ("4230", "Realized Crypto Gains",               "4200", "REVENUE", "INVESTMENT_REV",  "CREDIT", True,  None),
    ("4300", "Government Transfers",                "4000", "REVENUE", "OTHER_REV",       "CREDIT", True,  None),
    ("4900", "Other Income",                        "4000", "REVENUE", "OTHER_REV",       "CREDIT", True,  None),

    # ── 5xxx EXPENSES ──────────────────────────────────────────────────────
    ("5000", "EXPENSES",                            None,   "EXPENSE", None,                "DEBIT", False, None),
    # Living
    ("5100", "Living Expenses",                     "5000", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", False, None),
    ("5110", "F&B",                                 "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5111", "F&B (delivery)",                      "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5120", "Groceries",                           "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5130", "Transport",                           "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5131", "Transport (Public)",                  "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5132", "Transport (Fuel)",                    "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5140", "Utilities",                           "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", False, None),
    ("5141", "Utilities - Internet",                "5140", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5142", "Utilities - Mobile",                  "5140", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5143", "Utilities - Electricity",             "5140", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5150", "Healthcare",                          "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5160", "Shopping",                            "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5161", "Shopping (online)",                   "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5170", "Family Expense",                      "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5190", "General Expense (parked)",            "5100", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    # Subscriptions & professional
    ("5200", "Subscriptions & Tools",               "5000", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    # Insurance — P&L portion only (whole-life cash value goes to assets)
    ("5300", "Insurance Premium (P&L portion)",     "5000", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", False, None),
    ("5310", "Insurance - Term Life",               "5300", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5320", "Insurance - Critical Illness",        "5300", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5330", "Insurance - Health",                  "5300", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    ("5340", "Insurance - Whole Life (P&L slice)",  "5300", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    # Finance costs — interest only, NOT principal
    ("5400", "Finance Costs",                       "5000", "EXPENSE", "FINANCE_COST",     "DEBIT", False, None),
    ("5410", "Credit Card Interest",                "5400", "EXPENSE", "FINANCE_COST",     "DEBIT", True,  None),
    ("5420", "Term Loan Interest",                  "5400", "EXPENSE", "FINANCE_COST",     "DEBIT", True,  None),
    ("5430", "Moneylender Interest",                "5400", "EXPENSE", "FINANCE_COST",     "DEBIT", True,  None),
    ("5440", "Cashline / OD Interest",              "5400", "EXPENSE", "FINANCE_COST",     "DEBIT", True,  None),
    ("5450", "Late Payment Fees",                   "5400", "EXPENSE", "FINANCE_COST",     "DEBIT", True,  None),
    ("5460", "Annual Fees / Processing Fees",       "5400", "EXPENSE", "FINANCE_COST",     "DEBIT", True,  None),
    # Tax
    ("5500", "Tax",                                 "5000", "EXPENSE", "TAX",              "DEBIT", True,  None),
    # Government fees
    ("5600", "Government Fees",                     "5000", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
    # Bank fees
    ("5700", "Bank Fees",                           "5000", "EXPENSE", "OPERATING_EXPENSE", "DEBIT", True,  None),
]


def upsert_coa(s):
    """Idempotent upsert of COA_TREE. Resolves parent_code → parent_id in 2 passes."""
    from sqlalchemy import select
    now = db.now_utc()

    # Pass 1: insert / update all rows without parent_id linkage
    for code, name, parent_code, klass, subclass, normal, is_postable, sub_table in COA_TREE:
        existing = s.execute(
            select(ledger.ChartOfAccount).where(ledger.ChartOfAccount.account_code == code)
        ).scalar_one_or_none()
        fields = dict(
            account_name=name,
            account_class=klass,
            account_subclass=subclass,
            normal_balance=normal,
            is_postable=bool(is_postable),
            is_control_account=bool(sub_table),
            sub_ledger_table=sub_table,
            is_active=True,
        )
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            s.add(ledger.ChartOfAccount(
                account_code=code,
                created_at=now,
                **fields,
            ))
    s.commit()

    # Pass 2: link parent_id from parent_code lookups
    code_to_id = {row.account_code: row.id for row in
                  s.execute(select(ledger.ChartOfAccount)).scalars().all()}
    for code, name, parent_code, *_ in COA_TREE:
        if parent_code is None:
            continue
        parent_id = code_to_id.get(parent_code)
        if parent_id is None:
            print(f"  WARNING: parent {parent_code} not found for {code}", file=sys.stderr)
            continue
        row = s.execute(
            select(ledger.ChartOfAccount).where(ledger.ChartOfAccount.account_code == code)
        ).scalar_one()
        row.parent_id = parent_id
    s.commit()

    return len(COA_TREE)


def upsert_parties_from_classifier(s):
    """Bootstrap Party master from classifier.yaml vendors."""
    from sqlalchemy import select
    classifier_path = Path("/finance/classifier.yaml")
    if not classifier_path.exists():
        return 0
    data = yaml.safe_load(classifier_path.read_text()) or {}
    now = db.now_utc()

    # Map account_type → party_type
    party_type_map = {
        "income": "customer",       # whoever pays you
        "expense": "vendor",
        "transfer": "self",          # own accounts
        "liability": "lender",       # debt service goes to lenders
        "investment": "vendor",      # broker / asset manager
    }
    inserted = 0
    for v in data.get("vendors", []):
        canonical = v.get("canonical", "").strip()
        if not canonical:
            continue
        code = canonical.upper().replace(" ", "_")[:64]
        existing = s.execute(
            select(ledger.Party).where(ledger.Party.party_code == code)
        ).scalar_one_or_none()
        fields = dict(
            party_name=canonical,
            party_type=party_type_map.get(v.get("account_type", "expense"), "vendor"),
            classification=v.get("category"),
            is_active=True,
        )
        if existing:
            for k, val in fields.items():
                setattr(existing, k, val)
        else:
            s.add(ledger.Party(
                party_code=code,
                created_at=now,
                updated_at=now,
                **fields,
            ))
            inserted += 1
    s.commit()
    return inserted


def main():
    print("[ledger_seed] init_db (creates tables if missing) …")
    db.init_db()
    s = db.SessionLocal()
    try:
        n_coa = upsert_coa(s)
        print(f"[ledger_seed] upserted {n_coa} CoA accounts")
        n_parties = upsert_parties_from_classifier(s)
        print(f"[ledger_seed] upserted {n_parties} parties from classifier.yaml")
        # Quick summary
        from sqlalchemy import select, func
        for klass in ("ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"):
            n = s.execute(
                select(func.count(ledger.ChartOfAccount.id))
                .where(ledger.ChartOfAccount.account_class == klass)
            ).scalar_one()
            print(f"  {klass:<10}: {n} accounts")
    finally:
        s.close()


if __name__ == "__main__":
    main()
