"""SentinelLite General Ledger — proper double-entry bookkeeping.

This is the foundation for decoupling from Firefly III (planned v2.0).
At v1.10.0 the tables exist but Firefly is still the source of truth; the
bridge (v1.10.2) populates these tables from Firefly transactions.

Schema overview:

  parties           — master record for everyone you transact with
                      (vendors, customers, employers, self, tax authority)
  chart_of_accounts — IAS 1 hierarchical CoA, 5 classes:
                      ASSET / LIABILITY / EQUITY / REVENUE / EXPENSE
  journals          — header for each double-entry transaction
  general_ledger    — Dr/Cr lines (constraint: per-journal ΣDr = ΣCr)
  bank_reconciliation — periodic snapshot of bank balance ↔ GL balance
  investment_positions — sub-ledger for asset detail (ILP funds, CPF IS funds, crypto)

Sub-ledgers already in place (database.py):
  credit_facilities + facility_plans + payment_schedule + actual_payments
  → these serve as the liability sub-ledger (drives accrued interest, A+B=C+D)
"""
from __future__ import annotations

from sqlalchemy import (Column, Integer, String, Float, DateTime, Date, Index,
                        ForeignKey, CheckConstraint, Boolean)
from sqlalchemy.orm import relationship

from .database import Base, now_utc


# ── 1. Parties (vendor + customer + employer master) ────────────────────────


class Party(Base):
    """Master record for any counterparty in your transactions.

    A vendor card (`party_type='vendor'`) stores who you pay (Foodpanda,
    Anthropic, EZ Loan). A customer card (`party_type='customer'`) stores
    who pays you (employer, IRAS for tax refunds, CDP for dividends).

    Replaces the canonical-vendor concept in classifier.yaml with a proper
    relational master. The classifier still drives matching by description,
    but the canonical name resolves to a party_id.
    """
    __tablename__ = "parties"
    id = Column(Integer, primary_key=True)
    party_code = Column(String, unique=True, nullable=False)   # short slug e.g. 'FOODPANDA'
    party_name = Column(String, nullable=False)                # display name
    party_type = Column(String, nullable=False)                # vendor|customer|employer|self|tax_authority|bank|lender|other
    # Classification — broad category for grouping (e.g. 'F&B', 'Subscription',
    # 'Moneylender', 'Government'). Used for sub-totalling.
    classification = Column(String)
    # Default chart_of_accounts.id to post against when this party appears.
    # Optional — classifier rule or manual override can still pick a different account.
    default_account_id = Column(Integer)
    # Identity / regulatory
    country = Column(String, default="SG")
    address = Column(String)
    contact = Column(String)
    tax_id = Column(String)        # UEN, NRIC, etc.
    license_no = Column(String)    # e.g. moneylender license
    # State
    is_active = Column(Boolean, default=True, nullable=False)
    notes = Column(String)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_parties_type_name", "party_type", "party_name"),
    )


# ── 2. Chart of Accounts (IAS 1, hierarchical) ──────────────────────────────


class ChartOfAccount(Base):
    """Hierarchical CoA. Each row is a node; `parent_id` makes the tree.

    Codes follow conventional ranges:
      1xxx Assets             (normal balance = DEBIT)
      2xxx Liabilities        (normal balance = CREDIT)
      3xxx Equity             (normal balance = CREDIT)
      4xxx Revenue            (normal balance = CREDIT)
      5xxx Expenses           (normal balance = DEBIT)

    A 'postable' account is a leaf (no children) that GL lines can hit.
    Header accounts (with children) are aggregation-only.
    """
    __tablename__ = "chart_of_accounts"
    id = Column(Integer, primary_key=True)
    account_code = Column(String, unique=True, nullable=False)   # e.g. "1111"
    account_name = Column(String, nullable=False)                # display
    parent_id = Column(Integer)                                  # FK self
    # Classification
    account_class = Column(String, nullable=False)               # ASSET|LIABILITY|EQUITY|REVENUE|EXPENSE
    account_subclass = Column(String)                            # e.g. CURRENT_ASSET, NON_CURRENT_ASSET, OPERATING_EXPENSE, FINANCE_COST
    normal_balance = Column(String, nullable=False)              # DEBIT|CREDIT
    # State
    is_active = Column(Boolean, default=True, nullable=False)
    is_postable = Column(Boolean, default=True, nullable=False)  # False for header/parent accounts
    is_control_account = Column(Boolean, default=False)          # True if this account has a sub-ledger
    sub_ledger_table = Column(String)                            # e.g. 'credit_facilities', 'investment_positions'
    # Cross-references
    firefly_acct_id = Column(Integer)                            # for the v1 bridge
    iso_currency = Column(String, default="SGD")
    notes = Column(String)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_coa_parent", "parent_id"),
        Index("ix_coa_class", "account_class"),
        CheckConstraint("account_class IN ('ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE')",
                        name="ck_coa_class"),
        CheckConstraint("normal_balance IN ('DEBIT','CREDIT')",
                        name="ck_coa_normal_balance"),
    )


# ── 3. Journal (header for each double-entry transaction) ────────────────────


class Journal(Base):
    """One row per business transaction. Each journal has 2+ GL lines.

    Source documents (source_doc + source_ref) trace each journal back to its
    origin: a bank statement row, a manual entry, a Firefly tx, etc.

    Status flow:
      draft → posted → (optionally) voided
    Only `posted` journals affect balances.
    """
    __tablename__ = "journals"
    id = Column(Integer, primary_key=True)
    journal_no = Column(String, unique=True, nullable=False)     # e.g. "JNL-2026-00001"
    journal_date = Column(Date, nullable=False)                  # business date
    narration = Column(String)
    # Journal type — used for reporting and filtering
    journal_type = Column(String, nullable=False)                # cash_receipt|cash_payment|sales|purchase|general|accrual|reversal|opening|closing
    # Source document (provenance)
    source_doc = Column(String)                                  # 'POSB_CSV' | 'MAYBANK_CC_STMT' | 'FIREFLY' | 'MANUAL' | 'STATEMENT_PARSER'
    source_ref = Column(String)                                  # 'Firefly:5832' | 'posb_2026.csv:row42'
    external_id = Column(String)                                 # for de-dup (e.g. statement line hash)
    # State
    status = Column(String, nullable=False, default="posted")    # draft|posted|voided
    posted_at = Column(DateTime)
    voided_at = Column(DateTime)
    voided_reason = Column(String)
    created_by = Column(String, default="system")
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_journals_date", "journal_date"),
        Index("ix_journals_source", "source_doc", "source_ref"),
        Index("ix_journals_external_id", "external_id"),
        CheckConstraint("status IN ('draft','posted','voided')", name="ck_journal_status"),
    )


# ── 4. General Ledger (the Dr/Cr lines) ──────────────────────────────────────


class GeneralLedgerEntry(Base):
    """One line of a journal. Each entry is exactly one Dr OR one Cr (not both).

    Constraint enforced at application level (sum of debits == sum of credits
    per journal_id) — SQLite doesn't support deferrable check constraints
    natively, so we validate before commit in journal_service.post_journal().

    Multi-currency: `currency` + `fx_rate` capture the original ccy + rate,
    `debit_sgd`/`credit_sgd` are the converted amounts used in all reporting.
    """
    __tablename__ = "general_ledger"
    id = Column(Integer, primary_key=True)
    journal_id = Column(Integer, nullable=False, index=True)     # FK journals
    line_no = Column(Integer, nullable=False)                    # 1, 2, 3 within journal
    account_id = Column(Integer, nullable=False, index=True)     # FK chart_of_accounts
    party_id = Column(Integer, index=True)                       # FK parties (optional, for vendor/customer postings)
    # Amount (one of these is non-zero, the other is zero)
    debit = Column(Float, nullable=False, default=0.0)
    credit = Column(Float, nullable=False, default=0.0)
    # Multi-currency support
    currency = Column(String, default="SGD")
    fx_rate = Column(Float, default=1.0)                         # to SGD
    debit_sgd = Column(Float, nullable=False, default=0.0)
    credit_sgd = Column(Float, nullable=False, default=0.0)
    # Per-line narration
    narration = Column(String)
    # Sub-ledger linkage (optional)
    sub_ledger_table = Column(String)                            # e.g. 'credit_facilities'
    sub_ledger_id = Column(String)                               # e.g. 'sands-credit'
    sub_ledger_event = Column(String)                            # e.g. 'instalment_paid:9'
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_gl_journal_line", "journal_id", "line_no", unique=True),
        Index("ix_gl_account_date", "account_id"),
        Index("ix_gl_sub_ledger", "sub_ledger_table", "sub_ledger_id"),
        CheckConstraint("debit >= 0 AND credit >= 0", name="ck_gl_amount_nonneg"),
        CheckConstraint("(debit = 0 AND credit > 0) OR (credit = 0 AND debit > 0)",
                        name="ck_gl_one_side_only"),
    )


# ── 5. Bank reconciliation snapshots ─────────────────────────────────────────


class BankReconciliation(Base):
    """Periodic snapshot: bank statement balance vs GL balance for one bank account.

    One row per (account_id, reconciliation_date). Created automatically after
    each statement import. `variance` should be 0 when fully reconciled; non-zero
    triggers an alert in the credit-utilization-style reconciliation panel.
    """
    __tablename__ = "bank_reconciliation"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, nullable=False, index=True)     # FK chart_of_accounts
    reconciliation_date = Column(Date, nullable=False)
    statement_balance = Column(Float, nullable=False)
    ledger_balance = Column(Float, nullable=False)
    variance = Column(Float, nullable=False)                     # statement - ledger
    status = Column(String, nullable=False, default="matched")   # matched|in_progress|disputed
    statement_source = Column(String)                            # source PDF/CSV path
    notes = Column(String)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_bankrec_account_date", "account_id", "reconciliation_date", unique=True),
    )


# ── 6. Investment positions (sub-ledger for asset detail) ────────────────────


class InvestmentPosition(Base):
    """Sub-ledger row per investment holding. Links up to a CoA 'investment'
    account (e.g. '1215 ILP - Tokio Marine') and tracks the per-fund / per-token
    detail beneath it.

    For ILPs and CPF IS: one row per fund.
    For crypto: one row per (chain, token).
    Aggregates up to the parent CoA account; difference = revaluation gain/loss
    that posts to a separate equity-side account.
    """
    __tablename__ = "investment_positions"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, nullable=False, index=True)     # FK chart_of_accounts (the parent investment account)
    position_code = Column(String, nullable=False)               # fund ISIN, token contract, etc.
    position_name = Column(String, nullable=False)               # display
    # Cost & current
    quantity = Column(Float)                                     # units / shares / tokens
    cost_basis_sgd = Column(Float)                               # what was paid (history)
    current_value_sgd = Column(Float)                            # latest mark-to-market
    last_valued_at = Column(DateTime)
    valuation_source = Column(String)                            # Morningstar | Moralis | DexScreener | manual
    # Provenance
    notes = Column(String)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_invpos_account_code", "account_id", "position_code", unique=True),
    )


# ── 7. Firefly bridge map (transient, retires at v2.0) ───────────────────────


class FireflyBridgeMap(Base):
    """One-to-one mapping between our journal_id and Firefly's tx id.

    Used during the v1.10.x transition while Firefly remains the source of truth.
    The bridge sync writes each Firefly tx as a journal here, plus a mapping row.
    On v2 cutover, this table can be dropped along with Firefly.
    """
    __tablename__ = "firefly_bridge_map"
    id = Column(Integer, primary_key=True)
    journal_id = Column(Integer, unique=True, nullable=False)
    firefly_tx_id = Column(Integer, nullable=False)
    firefly_account_id = Column(Integer)
    sync_direction = Column(String, default="firefly_to_local")  # firefly_to_local|local_to_firefly|two_way
    last_synced_at = Column(DateTime, nullable=False)
    sync_hash = Column(String)                                   # for change detection

    __table_args__ = (
        Index("ix_bridge_firefly", "firefly_tx_id", unique=True),
    )


# ── 8. Statement Registry (per-parsed-statement metadata) ────────────────────


class StatementRegistry(Base):
    """One row per parsed credit-card / loan / bank statement.

    Lets the bot answer "what are my 12 statement dates for SC CC?" with a
    single SQL hit instead of re-parsing PDFs. Populated by cc_pipeline +
    backfill_registries.

    Idempotent insert: unique on (facility_id, statement_date).
    """
    __tablename__ = "statement_registry"
    id = Column(Integer, primary_key=True)
    facility_id = Column(String, nullable=False, index=True)     # FK to credit_facilities.id (loose)
    bank = Column(String, nullable=False)                        # 'dbs_cc' | 'maybank_cc' | etc.
    statement_date = Column(Date, nullable=False)                # cycle-end date
    period_start = Column(Date)
    period_end = Column(Date)
    previous_balance = Column(Float)
    closing_balance = Column(Float)
    minimum_due = Column(Float)
    payment_due_date = Column(Date)
    credit_limit = Column(Float)
    available_credit = Column(Float)
    line_count = Column(Integer)                                 # # of tx in this statement
    source_path = Column(String)                                 # PDF location (relative to OneDrive)
    parsed_at = Column(DateTime, nullable=False)
    extras = Column(String)                                      # JSON dump for bank-specific overflow
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_stmtreg_facility_date", "facility_id", "statement_date", unique=True),
        Index("ix_stmtreg_bank_date", "bank", "statement_date"),
    )


# ── 9. Payslip Registry (per-period payroll metadata) ────────────────────────


class PayslipRegistry(Base):
    """One row per payslip parsed. Powers bot queries like
    'what was my Dec 2025 gross?' or 'show all AZ United payslips'.

    Idempotent insert: unique on (employer_key, period_end).
    """
    __tablename__ = "payslip_registry"
    id = Column(Integer, primary_key=True)
    employer = Column(String, nullable=False)
    employer_key = Column(String, nullable=False, index=True)    # 'az_united', 'youragency', ...
    period_start = Column(Date)
    period_end = Column(Date, nullable=False)
    payment_date = Column(Date)
    basic_pay = Column(Float)
    allowances = Column(Float)
    gross_pay = Column(Float)
    employee_cpf = Column(Float)
    employer_cpf = Column(Float)
    fund_deductions = Column(Float)
    other_deductions = Column(Float)
    sdl = Column(Float)
    net_pay = Column(Float)
    journal_id = Column(Integer)                                 # FK to journals.id (loose) — the salary journal
    source_path = Column(String)
    parsed_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_paysreg_emp_period", "employer_key", "period_end", unique=True),
        Index("ix_paysreg_payment_date", "payment_date"),
    )


# ── 10. NAV History (per-fund per-day NAV from Morningstar / FSMone / manual) ──


class NavHistory(Base):
    """Time series of fund NAVs. Populated by morningstar_sg.refresh_all() on
    every daily run (currently only updates funds.yaml in-place — loses history).
    Lets the bot answer 'what was Tokio Marine ILP value at 2025-12-31?' by
    looking up NAV(s) at that date × current units.

    Idempotent insert: unique on (fund_id, nav_date).
    """
    __tablename__ = "nav_history"
    id = Column(Integer, primary_key=True)
    fund_id = Column(String, nullable=False, index=True)         # matches funds.yaml id
    fund_name = Column(String)
    nav_date = Column(Date, nullable=False, index=True)
    nav_price = Column(Float, nullable=False)
    currency = Column(String, default="SGD")
    source = Column(String)                                       # 'morningstar' | 'fsmone' | 'manual' | 'stmt'
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_nav_fund_date", "fund_id", "nav_date", unique=True),
    )


# ── 11b. Recurring Obligation Registry ───────────────────────────────────────


class RecurringObligation(Base):
    """Unified registry for recurring outflows: insurance premiums, ILP
    contributions, subscriptions, GIROs, standing instructions, term-loan
    instalments. Bank-statement reconciler matches each outflow against this
    registry to (a) post the correct journal and (b) flag amount drift.

    `kind` is informational: insurance | ilp | subscription | utility |
                              loan | tax | charity | other.

    Matching rule used by recurring_reconciler:
      1. tx.amount within `amount_tolerance` of `expected_amount`
      2. AND at least one `identifier_pattern` regex matches the tx narration
         OR the tx falls within `expected_day_of_month ± grace_days`
    """
    __tablename__ = "recurring_obligation_registry"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    kind = Column(String, nullable=False, index=True)
    contra_coa = Column(String, nullable=False)         # e.g. 5340, 12229, 2121, 2222
    direction = Column(String, default="out")           # 'out' (debit POSB) | 'in'
    expected_amount = Column(Float, nullable=False)
    amount_tolerance = Column(Float, default=0.50)
    frequency = Column(String, default="monthly")        # monthly | yearly | weekly | adhoc
    expected_day_of_month = Column(Integer)              # 1-31 or NULL
    grace_days = Column(Integer, default=5)
    identifier_patterns = Column(String)                  # JSON list of regex; e.g. '["SINGAPORE LIFE", "P4064051"]'
    counterparty_hint = Column(String)                    # display name
    journal_kind = Column(String, default="expense")     # 'expense'|'transfer'|'loan_pay'|'ilp_premium'
    active_from = Column(Date)
    active_to = Column(Date)                              # NULL = open-ended
    notes = Column(String)
    last_seen_journal_id = Column(Integer)
    last_seen_amount = Column(Float)
    last_seen_date = Column(Date)
    drift_alerts = Column(Integer, default=0)             # count of amount-mismatch events
    created_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())
    updated_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())

    __table_args__ = (
        Index("ix_recurob_kind_active", "kind", "active_from", "active_to"),
    )


class RecurringReconcileLog(Base):
    """Audit trail for recurring obligation matches.
    Three kinds of rows:
      - 'matched'       : tx successfully matched to a registry row, journal repointed
      - 'amount_drift'  : tx narration matched registry but amount differs > tolerance
      - 'orphan'        : recurring pattern detected (2+ hits same amount/payee) with
                          no registry hit → asks user to register the obligation
    """
    __tablename__ = "recurring_reconcile_log"
    id = Column(Integer, primary_key=True)
    status = Column(String, nullable=False, index=True)
    obligation_id = Column(Integer)
    journal_id = Column(Integer, index=True)
    voided_journal_id = Column(Integer)
    tx_date = Column(Date)
    tx_amount = Column(Float)
    expected_amount = Column(Float)
    counterparty = Column(String)
    notes = Column(String)
    created_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())


# ── 11. OCR Normalize Log ────────────────────────────────────────────────────


class OcrNormalizeLog(Base):
    """One row per (source_hash) — caches the canonical word-list extraction
    for any document type (PDF text, PDF image, JPG, PNG, HEIC, etc).

    Hash-keyed so renames/moves don't trigger re-OCR. mtime-cached for
    incremental updates. Confidence field surfaces low-quality OCR for review.
    """
    __tablename__ = "ocr_normalize_log"
    id = Column(Integer, primary_key=True)
    source_hash = Column(String, nullable=False, unique=True, index=True)
    source_path = Column(String, nullable=False)         # last-seen path (informational)
    source_mtime = Column(DateTime)                       # source file mtime at extraction
    source_size = Column(Integer)
    file_format = Column(String)                          # 'pdf' | 'jpg' | 'png' | 'heic' | ...
    extraction_method = Column(String)                    # 'pdfplumber' | 'tesseract'
    ocr_engine = Column(String)                           # 'tesseract-5.x.x' or NULL
    languages = Column(String)                            # 'eng+chi_sim'
    page_count = Column(Integer)
    word_count = Column(Integer)
    min_confidence = Column(Float)                        # 0–1.0; NULL for text-PDF
    cache_path = Column(String)                           # /data/ocr_cache/<hash>.ocr.json
    status = Column(String, nullable=False, index=True)   # 'ready' | 'failed' | 'low_confidence'
    error_msg = Column(String)
    extracted_at = Column(DateTime, nullable=False)


# ── 12. Salary Reconcile Log ─────────────────────────────────────────────────


class SalaryReconcileLog(Base):
    """Audit trail for cross-pipeline salary reconciliation.

    Three kinds of rows:
      - 'matched_dup': payslip journal supersedes POSB cutover suspense; the
        POSB-side journal is voided. Stores both jids for traceability.
      - 'missing_payslip': POSB salary inflow exists but no payslip on file.
        Sentinel chases the user to upload the payslip PDF.
      - 'orphan_payslip': payslip journal exists but no POSB inflow on the
        same date+amount. Probably timing offset; review manually.
    """
    __tablename__ = "salary_reconcile_log"
    id = Column(Integer, primary_key=True)
    status = Column(String, nullable=False, index=True)
        # 'matched_dup' | 'missing_payslip' | 'orphan_payslip'
    payslip_id = Column(Integer, index=True)        # FK loose to payslip_registry.id
    payslip_journal_id = Column(Integer)            # FK loose to journals.id (the PAYSLIP-sourced one)
    posb_journal_id = Column(Integer, index=True)   # FK loose to journals.id (the POSB_PDF_DIRECT one)
    voided_journal_id = Column(Integer)             # the journal we voided (=posb_journal_id when matched_dup)
    period_end = Column(Date)
    amount = Column(Float)
    employer_guess = Column(String)                  # 'AZ UNITED PTE LTD' | NULL
    notes = Column(String)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_salrecon_status_period", "status", "period_end"),
    )


# ── 13. Insurance Policy Registry (insurance + ILP policies) ─────────────────


class InsurancePolicyRegistry(Base):
    """Canonical registry for insurance + ILP policies.

    `kind` distinguishes the accounting treatment:
      - 'term_life' / 'whole_life' / 'health' / 'critical_illness' → P&L expense (5310/5320/5330/5340)
      - 'ilp'                                                      → asset accumulation (12229 etc.)
                                                                     plus optional P&L slice
    """
    __tablename__ = "insurance_policy_registry"
    id = Column(Integer, primary_key=True)
    policy_ref = Column(String, nullable=False, unique=True, index=True)
    insurer = Column(String, nullable=False)
    product_name = Column(String)
    kind = Column(String, nullable=False, index=True)
    premium_amount = Column(Float, nullable=False)
    premium_frequency = Column(String, default="monthly")   # monthly | quarterly | annual
    premium_currency = Column(String, default="SGD")
    due_day = Column(Integer)                                # 1-31 if monthly
    start_date = Column(Date)
    end_date = Column(Date)
    next_due_date = Column(Date)
    contra_coa = Column(String, nullable=False)              # 5310 / 12229 / etc.
    contra_coa_pnl_slice = Column(String)                    # for ILP whole-life P&L portion (5340)
    identifier_patterns = Column(String)                      # JSON list (regex)
    counterparty_hint = Column(String)
    source_doc_path = Column(String)
    status = Column(String, default="active", index=True)    # active | lapsed | surrendered | matured
    notes = Column(String)
    created_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())
    updated_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())

    __table_args__ = (Index("ix_polreg_kind_status", "kind", "status"),)


# ── 14. ILP Portfolio Snapshot (NAV + units per policy per date) ─────────────


class IlpPortfolioSnapshot(Base):
    """Time series of ILP NAV / units from periodic portfolio statements.

    Joins to InsurancePolicyRegistry by policy_ref. The latest snapshot is the
    canonical 'current value' of the ILP asset on the balance sheet.
    Premium-side journal goes through GL (Dr 12229); revaluation gap between
    cumulative premiums and NAV gets posted to 3300 OCI on snapshot ingest.
    """
    __tablename__ = "ilp_portfolio_snapshot"
    id = Column(Integer, primary_key=True)
    policy_ref = Column(String, nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False)
    units_held = Column(Float)
    nav_per_unit = Column(Float)
    total_value = Column(Float, nullable=False)
    currency = Column(String, default="SGD")
    source_doc_path = Column(String)
    parsed_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())

    __table_args__ = (Index("ix_ilpsnap_policy_date", "policy_ref", "snapshot_date", unique=True),)


# ── 15. Subscription Registry (ChatGPT, Apple, etc.) ─────────────────────────


class SubscriptionRegistry(Base):
    """Canonical registry for software / media / utility subscriptions.

    Distinct from insurance (long-term contract) and credit_facilities (debt).
    Source doc: renewal email or subscription confirmation. Verifier uses
    `identifier_patterns` (vendor name) + amount to match POSB outflows.
    """
    __tablename__ = "subscription_registry"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    vendor = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="SGD")
    frequency = Column(String, default="monthly")            # monthly | annual
    billing_method = Column(String)                           # credit_card | giro | paynow
    contra_coa = Column(String, nullable=False, default="5200")
    identifier_patterns = Column(String)                       # JSON list
    next_due_date = Column(Date)
    start_date = Column(Date)
    cancelled_date = Column(Date)
    status = Column(String, default="active", index=True)    # active | cancelled
    notes = Column(String)
    created_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())
    updated_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())


# ── 16. CPF Statement Registry ───────────────────────────────────────────────


class CpfStatementRegistry(Base):
    """One row per monthly CPF statement.
    Records snapshot balances + contributions + interest credit per month.
    Verifier checks: SUM(payslip employer+employee CPF for month) ==
                     CpfStatementRegistry.employer_contrib + employee_contrib
                     for the same month.
    """
    __tablename__ = "cpf_statement_registry"
    id = Column(Integer, primary_key=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    balance_oa = Column(Float)
    balance_sa = Column(Float)
    balance_ma = Column(Float)
    balance_is = Column(Float)                                # CPF Investment Scheme
    employer_contribution = Column(Float)
    employee_contribution = Column(Float)
    interest_credited = Column(Float)
    transfers_out = Column(Float)                             # housing draw, etc.
    source_doc_path = Column(String)
    parsed_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())

    __table_args__ = (Index("ix_cpfreg_period", "period_end", unique=True),)


# ── 17. Unreconciled Queue (pre-posting verifier output) ─────────────────────


class UnreconciledQueue(Base):
    """Each candidate journal that the verifier couldn't auto-post (confidence
    below threshold) lands here. User triages on /reconcile and resolves.

    Resolution writes to GL (if approved) AND back to the underlying registry
    (if the user assigns the row to a new obligation / subscription / etc.).
    """
    __tablename__ = "unreconciled_queue"
    id = Column(Integer, primary_key=True)
    source_doc = Column(String, nullable=False, index=True)   # POSB_PDF_DIRECT | CC_PDF_DIRECT:NNNN | ...
    source_ref = Column(String)                                # PDF path + line index
    tx_date = Column(Date, nullable=False, index=True)
    tx_amount = Column(Float, nullable=False)
    tx_narration = Column(String)
    tx_carriers = Column(String)                               # JSON
    tx_type = Column(String)
    direction = Column(String)                                 # 'in' | 'out'
    candidate_journal = Column(String, nullable=False)         # JSON of proposed legs
    best_guess_matches = Column(String)                        # JSON list, top-3 by confidence
    confidence = Column(Integer, default=0)
    status = Column(String, nullable=False, default="pending", index=True)
                                                               # pending | resolved | rejected
    user_decision = Column(String)                             # CoA / registry_kind:row_id / 'spam' / etc.
    resolved_at = Column(DateTime)
    posted_journal_id = Column(Integer)                        # FK loose to journals.id
    external_id = Column(String, unique=True, index=True)      # idempotency for re-ingestion
    notes = Column(String)
    created_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())

    __table_args__ = (Index("ix_unrec_status_date", "status", "tx_date"),)


# ── 18. Account Opening Anchor (Gate 1) ──────────────────────────────────────


class BankStatementRegistry(Base):
    """One row per parsed bank-account statement (asset side — POSB, Maybank,
    SC, Wise, etc.). Distinct from `statement_registry` which is CC/loan side.

    Every parser/cutover MUST write to this table after parsing. The
    period-reconciliation job (Gate 4) then validates
        GL_balance(account_code, as_of=period_end) == balance_carried_forward
    Drift → unreconciled_queue (reason='period_drift').

    Dashboard balance resolver (Gate 5) reads MAX(period_end).CF here
    instead of summing GL journals.
    """
    __tablename__ = "bank_statement_registry"
    id = Column(Integer, primary_key=True)
    account_code = Column(String, nullable=False, index=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    balance_brought_forward = Column(Float)
    balance_carried_forward = Column(Float, nullable=False)
    currency = Column(String, default="SGD")
    source_doc_path = Column(String, nullable=False)
    parsed_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())
    external_id = Column(String, unique=True, index=True)
    notes = Column(String)

    __table_args__ = (Index("ix_bsr_acct_period", "account_code", "period_end", unique=True),)


class AccountOpeningAnchor(Base):
    """The Day-0 row for every balance-sheet account. Enforces that you
    cannot post a transactional journal on an asset/liability/equity account
    until its opening balance has been registered.

    Gap between BF and zero (e.g. POSB had $2,338 the day we started
    journaling) lands in Retained Earnings (3100): historical net worth
    accumulated before this system existed.

    Invariant enforced in journal_service.post_journal:
        For each line.account_id where account_class in (ASSET, LIABILITY, EQUITY):
            EXISTS account_opening_anchor (account_id, opening_date ≤ journal_date)
        UNLESS journal_type == 'opening' itself.
    """
    __tablename__ = "account_opening_anchor"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, nullable=False, index=True)
    opening_date = Column(Date, nullable=False)
    opening_balance = Column(Float, nullable=False, default=0.0)
    source_doc = Column(String, nullable=False)
    source_ref = Column(String)
    posted_journal_id = Column(Integer)
    notes = Column(String)
    created_at = Column(DateTime, nullable=False, default=lambda: __import__("datetime").datetime.utcnow())

    __table_args__ = (Index("ix_aoa_acct_date", "account_id", "opening_date", unique=True),)


class AccountSnapshot(Base):
    """Periodic-sample snapshot — the SoT for ALL Class B accounts (audit-6 Q3).

    Generalises the audit-5 cex_snapshot. One table covers every account whose
    balance comes from a periodic API sample:
      - Coinbase / Binance / Bybit / Kraken     → source_type='cex'
      - Wise / Revolut / Trust SG / GXS         → source_type='bank_api'
      - DeFi position aggregators (Krystal etc) → source_type='defi_api'

    The resolver only cares about `account_code` + `captured_at`. The
    `source_type` differentiates UI presentation (risk badges, label) and
    drives writer-specific freshness rules — but Gate 5 reads from this
    one table uniformly. Pass-6 Q3 explicitly chose generic over a class-D
    proliferation (anchor_class D/E/F/G per source type).

    Contract:
      - A periodic job calls the source API and INSERTS one row per success.
      - _resolve_class_b reads MAX(captured_at) for the account_code.
      - Three explicit outcomes: snapshot / stale_snapshot / no_snapshot.
      - GL is NEVER a fallback path.

    `provider` is a free-text identifier within the source_type
    (e.g. 'coinbase', 'binance', 'wise', 'revolut') for telemetry and
    multi-tenant reporting.
    """
    __tablename__ = "account_snapshot"
    id = Column(Integer, primary_key=True)
    account_code = Column(String, nullable=False, index=True)
    source_type = Column(String, nullable=False)   # 'cex' | 'bank_api' | 'defi_api'
    provider = Column(String, nullable=False)      # 'coinbase' | 'wise' | ...
    captured_at = Column(DateTime, nullable=False, index=True)
    sgd_value = Column(Float, nullable=False)
    usd_value = Column(Float)
    fx_usd_sgd = Column(Float)
    # Audit-8 Q2: retain raw per-currency position so we can evolve to
    # FRS-21 FX P&L treatment in V3 without losing data captured in V2.
    # For single-currency snapshots, raw_amount == sgd_value (or usd_value)
    # and raw_currency is the corresponding ISO code.
    # For multi-currency aggregators (Wise summing 4 currencies), raw_amount
    # is NULL and raw_currencies (JSON) holds the breakdown.
    raw_currency = Column(String)                  # 'SGD' | 'USD' | NULL if aggregated
    raw_amount = Column(Float)                     # in raw_currency units; NULL if aggregated
    raw_currencies = Column(String)                # JSON list of {currency, amount, rate_to_sgd}
    source = Column(String, nullable=False)        # e.g. 'coinbase_cdp_api'
    external_ref = Column(String)                  # provider-side portfolio/account id
    raw_response = Column(String)                  # JSON for audit, nullable
    notes = Column(String)

    __table_args__ = (
        Index("ix_acct_snap_time", "account_code", "captured_at"),
    )


# Backwards-compat alias — keep code that imports CexSnapshot working until
# all references are migrated. Remove once `git grep CexSnapshot` is clean.
CexSnapshot = AccountSnapshot


class Alert(Base):
    """Pass-7 Q3: behavioural alerts module — separate from invariants.

    Invariants prove correctness of state. Alerts surface BEHAVIOURAL
    surprises that don't break correctness but warrant user attention:
      - Class A statement >90 days stale
      - Recurring obligation missed this month
      - Snapshot value dropped >X% week-over-week
      - Salary credit missing in the current month
      - Spend in a category > N standard deviations from baseline

    Written by `app/alerts.py` scan job. Dedupes by (kind, account_code,
    period) — re-running the scan updates existing rows, doesn't multiply.

    High-severity alerts deliver via Telegram on first detection;
    low/medium stay in the dashboard.
    """
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False, index=True)        # e.g. 'stale_class_a', 'missing_recurring'
    severity = Column(String, nullable=False, index=True)    # 'low' | 'medium' | 'high'
    account_code = Column(String, index=True)                # optional, scoped to one CoA
    period = Column(String)                                   # ISO date or YYYY-MM, optional
    message = Column(String, nullable=False)                  # human-readable
    details_json = Column(String)                             # blob for the detector's evidence
    status = Column(String, nullable=False, default="pending", index=True)
                                                              # pending | dismissed | resolved
    detected_at = Column(DateTime, nullable=False,
                         default=lambda: __import__("datetime").datetime.utcnow())
    notified_at = Column(DateTime)                            # when Telegram push went out
    dismissed_at = Column(DateTime)
    resolved_at = Column(DateTime)
    notes = Column(String)

    __table_args__ = (
        Index("ix_alert_kind_acct_period", "kind", "account_code", "period", unique=True),
    )


class CounterAccountMap(Base):
    """Per Perplexity audit-5 #3: the hardcoded
        {"1111": ["1113","1114","1115","1231"], "1114": ["1111"], "1115": ["1111"]}
    dict inside classify_drift() was data shaped like code. Moving it to a
    DB table buys:

      - SQL-visible config (inspectable in admin / sqlite3 shell)
      - Future-proof for an admin UI to edit corridors without redeploy
      - active_from/active_to for corridor lifecycle
      - audit-trail metadata per mapping

    For a PERIOD_DRIFT on `src_account_code`, the classify_drift() routine
    looks up every row here, collects the `dst_account_code` values, and
    asks: does any of them have a bank_statement_registry row whose period
    overlaps the drift's period_end? If so → fixable (T3); else → unresolvable
    (T2).

    Invariants (inv19b/c):
      - Both codes exist in chart_of_accounts.
      - Both rows have anchor_class='A' (statement-anchored) and
        account_class='ASSET' — peer-statement coverage only makes sense
        for accounts that have statements.
      - src_account_code != dst_account_code (no self-mapping).
      - Every (src,dst,relation_type) is unique.
    """
    __tablename__ = "counter_account_map"
    id = Column(Integer, primary_key=True)
    src_account_code = Column(String, nullable=False, index=True)
    dst_account_code = Column(String, nullable=False, index=True)
    relation_type = Column(String, nullable=False)
    active_from = Column(Date)
    active_to = Column(Date)
    notes = Column(String)
    created_at = Column(DateTime, nullable=False,
                        default=lambda: __import__("datetime").datetime.utcnow())

    __table_args__ = (
        Index("ix_cam_src_dst_rel", "src_account_code", "dst_account_code",
              "relation_type", unique=True),
        CheckConstraint("src_account_code != dst_account_code",
                        name="ck_cam_no_self"),
    )
