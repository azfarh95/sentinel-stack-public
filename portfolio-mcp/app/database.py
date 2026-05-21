"""SQLite schema for portfolio snapshots. One row per (snapshot, token)."""
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Index
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.environ.get("PORTFOLIO_DB", "/data/portfolio.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


@contextmanager
def session_scope():
    """Unit-of-work context manager. Replaces the repeated
    `s = SessionLocal(); try: ...; finally: s.close()` pattern.

    Usage:
        with db.session_scope() as s:
            row = s.execute(...).fetchone()
            # auto-commit on clean exit, auto-rollback on exception, always close
    """
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


class Snapshot(Base):
    __tablename__ = "snapshots"
    id = Column(Integer, primary_key=True)
    address = Column(String, nullable=False)
    captured_at = Column(DateTime, nullable=False)
    total_usd = Column(Float, nullable=False)
    chain_count = Column(Integer, default=0)
    token_count = Column(Integer, default=0)

    __table_args__ = (
        Index("ix_snapshots_addr_time", "address", "captured_at"),
    )


class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, nullable=False, index=True)
    chain = Column(String, nullable=False)
    token_address = Column(String)  # null for native
    symbol = Column(String, nullable=False)
    decimals = Column(Integer, default=18)
    raw_balance = Column(String, nullable=False)  # store as string to preserve big ints
    usd_price = Column(Float)
    usd_value = Column(Float, nullable=False)


class LastSeenTx(Base):
    """Bookmark per (address, chain) of the most recent tx hash we've alerted on.
    Polling loop uses this to skip already-notified transactions."""
    __tablename__ = "last_seen_tx"
    id = Column(Integer, primary_key=True)
    address = Column(String, nullable=False)
    chain = Column(String, nullable=False)
    last_tx_hash = Column(String)
    last_block_timestamp = Column(DateTime)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_lastseen_addr_chain", "address", "chain", unique=True),
    )


class ManualPosition(Base):
    """User-maintained DeFi positions that Moralis can't see (staking, LP, vaults).

    Two modes:
      A) Static USD (legacy): set `usd_value` directly. Stays put until manually updated.
      B) Auto-priced: set `token_chain`, `token_address`, `token_amount`, `token_symbol`.
         Daily price-refresh job recomputes usd_value = amount × dexscreener_price.
         AMOUNT only changes when user (un)stakes; PRICE tracks live.
    """
    __tablename__ = "manual_positions"
    id = Column(Integer, primary_key=True)
    label = Column(String, nullable=False, unique=True)  # e.g. "WolfSwap PACK stake"
    chain = Column(String, nullable=False)
    protocol = Column(String)  # e.g. "WolfSwap"
    usd_value = Column(Float, nullable=False)
    notes = Column(String)
    updated_at = Column(DateTime, nullable=False)
    # Auto-pricing fields (nullable for legacy entries)
    token_chain = Column(String)        # DexScreener chain slug: cronos, bsc, ethereum, polygon, ...
    token_address = Column(String)      # ERC20 contract address (lowercase)
    token_amount = Column(String)       # decimal as string (preserves precision)
    token_symbol = Column(String)       # display only
    last_price_usd = Column(Float)      # last fetched price per token
    last_priced_at = Column(DateTime)   # when last_price_usd was fetched


class User(Base):
    """Sentinel Finance user. Identity comes from Telegram Login Widget
    (HMAC-signed by our bot token). First login creates the row with
    status='pending'; admin must approve before access is granted."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(Integer, nullable=False, unique=True, index=True)
    telegram_username = Column(String)
    name = Column(String)
    picture_url = Column(String)
    email = Column(String, index=True)             # optional, for display only
    role = Column(String, nullable=False, default="member")   # admin | member
    status = Column(String, nullable=False, default="pending")  # pending | active | suspended | denied
    totp_secret = Column(String)
    totp_enabled_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    last_login_at = Column(DateTime)
    approved_at = Column(DateTime)
    approved_by_id = Column(Integer)


class Session(Base):
    """Server-side session record. Cookie carries the random `id` only;
    the user_id + expiry live here so we can revoke or invalidate sessions."""
    __tablename__ = "sessions"
    id = Column(String, primary_key=True)  # 32-byte url-safe random
    user_id = Column(Integer, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)
    ip = Column(String)
    user_agent = Column(String)
    totp_verified = Column(Integer, default=0)     # 0 = pending, 1 = passed TOTP this session


class HiddenToken(Base):
    """User-flagged spam tokens. Matched by (chain, token_address) on snapshot."""
    __tablename__ = "hidden_tokens"
    id = Column(Integer, primary_key=True)
    chain = Column(String, nullable=False)
    token_address = Column(String, nullable=False)
    symbol = Column(String)
    reason = Column(String)
    hidden_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_hidden_chain_addr", "chain", "token_address", unique=True),
    )


class ImportLog(Base):
    """One row per CSV/PDF auto-import attempt. Drives /config/imports history page."""
    __tablename__ = "import_log"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=False)
    source = Column(String, nullable=False)          # e.g. "posb_ibanking", "maybank_sav"
    file_name = Column(String, nullable=False)
    account_id = Column(Integer)                     # Firefly asset account id
    account_name = Column(String)
    n_rows = Column(Integer, default=0)
    created = Column(Integer, default=0)
    duplicates = Column(Integer, default=0)
    errored = Column(Integer, default=0)
    ledger_balance = Column(Float)                   # statement balance from CSV
    firefly_balance = Column(Float)                  # Firefly current_balance after import
    variance = Column(Float)                         # firefly - ledger
    error_summary = Column(String)                   # first few errors, truncated
    moved_to = Column(String)                        # path after processing
    triggered_by = Column(String, default="manual")  # manual | hourly_watcher | startup


class CreditFacility(Base):
    """One row per credit facility (CC, cashline, term loan, moneylender, BT, BNPL).

    Source of truth: this table. liabilities-registry.yaml is bootstrap-only.
    Once seeded, all updates go through admin UI or migration scripts — YAML edits
    are advisory.
    """
    __tablename__ = "credit_facilities"
    id = Column(String, primary_key=True)  # slug like 'sands-credit-16125'
    firefly_acct_id = Column(Integer, unique=True)  # nullable for paid-off facilities
    # Lender entity
    lender_name = Column(String, nullable=False)
    lender_uen = Column(String)
    lender_license = Column(String)
    lender_address = Column(String)
    lender_contact = Column(String)
    # Classification
    facility_type = Column(String, nullable=False)   # moneylender_loan|credit_card|revolving|term_loan|balance_transfer|line_of_credit|digital_loan|bnpl
    account_number = Column(String)
    # Dates
    origination_date = Column(DateTime)
    maturity_date = Column(DateTime)
    # Money
    principal_amount = Column(Float)
    disbursed_amount = Column(Float)
    admin_fee = Column(Float)
    nominal_monthly_pct = Column(Float)       # nominal interest, per month
    interest_basis = Column(String)            # reducing_balance|flat|revolving|unknown
    eir_pct = Column(Float)                    # effective interest rate p.a.
    late_fee = Column(Float)
    statement_fee = Column(Float)
    num_instalments = Column(Integer)
    instalment_amount = Column(Float)
    billing_day = Column(Integer)
    # State
    status = Column(String, nullable=False, default="active")  # active|paid_off|in_default|restructured|cancelled
    credit_limit = Column(Float)
    available_balance = Column(Float)
    current_outstanding = Column(Float)
    # Provenance
    agreement_document_path = Column(String)
    notes = Column(String)
    # Shared-limit relationships (e.g. SC BT shares limit with SC CC; Maybank
    # CreditAble 3 accounts share one $7k limit). When set, this facility's
    # outstanding is INCLUDED in the limit-bearing parent's reconciliation.
    shared_limit_with = Column(String)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class FacilityPlan(Base):
    """Active instalment plan (or revolving min-payment line) on a credit facility.
    Used for CC plans like 'DBS My Preferred Payment Plan' and revolving balances.

    A facility can have 0..N plans:
      - Term loans typically have 1 plan (the loan itself)
      - CCs have multiple plans + 1 revolving line
      - Revolving-only facilities have just 1 plan of kind='revolving_min'

    Drives the reconciliation: Σ(plan.outstanding) + revolving = facility.current_outstanding
    """
    __tablename__ = "facility_plans"
    id = Column(Integer, primary_key=True)
    facility_id = Column(String, nullable=False, index=True)
    plan_id = Column(String, nullable=False)  # YAML id (e.g. 'dbs-cc-007')
    plan_code = Column(String)                 # human-readable from statement
    kind = Column(String, nullable=False)      # instalment | revolving_min
    principal = Column(Float)                  # original principal
    monthly = Column(Float)                    # fixed monthly payment
    original_months = Column(Integer)
    remaining_months = Column(Integer)
    outstanding = Column(Float)                # as reported (may include unaccrued future interest)
    source = Column(String)
    # NEW v1.9.22: interest model so we can compute principal-only outstanding
    interest_rate_annual = Column(Float)       # e.g. 2.68 for 2.68% PA
    interest_method = Column(String)           # flat | reducing_balance | promo_zero | none
    processing_fee_pct = Column(Float)         # e.g. 1.0 for 1% one-time fee
    # Derived (recomputed by seed):
    principal_outstanding = Column(Float)      # current principal-only balance
    future_interest_remaining = Column(Float)  # interest yet to be charged on this plan


class PaymentSchedule(Base):
    """Planned instalment for a fixed-term facility. One row per period.
    Drives the interest/principal split for proper P&L reporting.
    """
    __tablename__ = "payment_schedule"
    id = Column(Integer, primary_key=True)
    facility_id = Column(String, nullable=False, index=True)  # FK -> credit_facilities.id
    instalment_no = Column(Integer, nullable=False)
    due_date = Column(DateTime, nullable=False)
    amount = Column(Float, nullable=False)
    principal_portion = Column(Float)
    interest_portion = Column(Float)
    status = Column(String, nullable=False, default="pending")  # pending|paid|late|missed|partial

    __table_args__ = (
        Index("ix_schedule_facility_instno", "facility_id", "instalment_no", unique=True),
    )


class ActualPayment(Base):
    """Recorded actual payment toward a facility. Links to a Firefly tx ID
    (the POSB/Maybank/SC withdrawal that paid the instalment).
    """
    __tablename__ = "actual_payments"
    id = Column(Integer, primary_key=True)
    facility_id = Column(String, nullable=False, index=True)
    schedule_id = Column(Integer, index=True)        # null for ad-hoc payments
    firefly_tx_id = Column(Integer, index=True)
    paid_date = Column(DateTime, nullable=False)
    amount = Column(Float, nullable=False)
    source_account = Column(String)                  # POSB Savings, Maybank Savings, etc.
    notes = Column(String)
    created_at = Column(DateTime, nullable=False)


class NetWorthSnapshot(Base):
    """Daily snapshot of headline totals. Feeds the future home sparkline + history chart."""
    __tablename__ = "networth_history"
    id = Column(Integer, primary_key=True)
    captured_at = Column(DateTime, nullable=False)
    snapshot_date = Column(String, nullable=False, index=True)  # YYYY-MM-DD, unique per day
    net_worth_sgd = Column(Float, nullable=False)
    net_worth_usd = Column(Float, nullable=False)
    assets_sgd = Column(Float)
    liabilities_sgd = Column(Float)
    bank_sgd = Column(Float)
    crypto_sgd = Column(Float)
    ilp_sgd = Column(Float)
    cpf_sgd = Column(Float)
    cc_sgd = Column(Float)
    loans_sgd = Column(Float)
    usd_to_sgd = Column(Float)


def init_db():
    # Import ledger models so SQLAlchemy registers them with Base.metadata.
    # MUST be done before create_all() — otherwise the tables don't get created.
    from . import ledger as _ledger  # noqa: F401
    from sqlalchemy import text, inspect
    # One-shot migration: drop legacy users/sessions tables built with the
    # Google-OAuth schema (email NOT NULL UNIQUE) so create_all rebuilds them
    # with the Telegram-Login schema (telegram_user_id NOT NULL UNIQUE).
    insp = inspect(engine)
    if "users" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("users")}
        if "telegram_user_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS sessions"))
                conn.execute(text("DROP TABLE IF EXISTS users"))

    Base.metadata.create_all(bind=engine)

    # SQLite ALTER for new auto-pricing columns on manual_positions
    new_cols = [
        ("token_chain", "VARCHAR"),
        ("token_address", "VARCHAR"),
        ("token_amount", "VARCHAR"),
        ("token_symbol", "VARCHAR"),
        ("last_price_usd", "FLOAT"),
        ("last_priced_at", "DATETIME"),
    ]
    with engine.begin() as conn:
        existing = {r[1] for r in conn.execute(text("PRAGMA table_info(manual_positions)")).fetchall()}
        for col, typ in new_cols:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE manual_positions ADD COLUMN {col} {typ}"))

    # Audit-6 Q1: partial UNIQUE index on journals.external_id WHERE status != 'voided'.
    # Voided journals can share their external_id with the replacement post
    # (e.g. amends after a void). Non-voided journals MUST be unique to prevent
    # the [direct POSB] / [POSB v2] cutover-collision bug (lost $60k phantom).
    with engine.begin() as conn:
        conn.execute(text("""
          CREATE UNIQUE INDEX IF NOT EXISTS ux_journals_external_id_active
          ON journals(external_id) WHERE status != 'voided' AND external_id IS NOT NULL
        """))

    # Audit-8 Q2: add per-currency columns to account_snapshot for future
    # FRS-21 FX work. Safe to ALTER ADD — NULLable.
    with engine.begin() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()}
        if "account_snapshot" in tables:
            cols = {r[1] for r in conn.execute(text(
                "PRAGMA table_info(account_snapshot)"
            )).fetchall()}
            for col, typ in [("raw_currency", "VARCHAR"),
                             ("raw_amount", "FLOAT"),
                             ("raw_currencies", "VARCHAR")]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE account_snapshot ADD COLUMN {col} {typ}"))

    # Audit-6 Q3: migrate cex_snapshot → account_snapshot (generic).
    # Base.metadata.create_all() above will have CREATEd `account_snapshot`
    # already (empty) if this is the first deploy after the rename. The
    # legacy `cex_snapshot` table may also still exist with data. Handle
    # both cases: copy old rows into the new table, then drop the legacy
    # table. Idempotent — safe to re-run.
    with engine.begin() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()}
        if "cex_snapshot" in tables:
            if "account_snapshot" in tables:
                # Copy rows, then drop the old table.
                conn.execute(text("""
                  INSERT INTO account_snapshot
                    (account_code, source_type, provider, captured_at, sgd_value,
                     usd_value, fx_usd_sgd, source, external_ref, raw_response, notes)
                  SELECT account_code, 'cex', exchange, captured_at, sgd_value,
                         usd_value, fx_usd_sgd, source, external_ref, raw_response, notes
                  FROM cex_snapshot
                """))
                conn.execute(text("DROP TABLE cex_snapshot"))
            else:
                # account_snapshot wasn't created yet — rename + backfill.
                conn.execute(text("ALTER TABLE cex_snapshot RENAME TO account_snapshot"))
                cols = {r[1] for r in conn.execute(text(
                    "PRAGMA table_info(account_snapshot)"
                )).fetchall()}
                if "source_type" not in cols:
                    conn.execute(text("ALTER TABLE account_snapshot ADD COLUMN source_type VARCHAR DEFAULT 'cex'"))
                if "provider" not in cols:
                    conn.execute(text("ALTER TABLE account_snapshot ADD COLUMN provider VARCHAR"))
                    conn.execute(text("UPDATE account_snapshot SET provider=exchange WHERE provider IS NULL"))
                conn.execute(text("DROP INDEX IF EXISTS ix_cex_acct_time"))
                conn.execute(text("""
                  CREATE INDEX IF NOT EXISTS ix_acct_snap_time
                  ON account_snapshot(account_code, captured_at)
                """))

    # v1.9.22: shared_limit_with on credit_facilities + interest fields on facility_plans
    with engine.begin() as conn:
        cf_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(credit_facilities)")).fetchall()}
        if cf_cols and "shared_limit_with" not in cf_cols:
            conn.execute(text("ALTER TABLE credit_facilities ADD COLUMN shared_limit_with VARCHAR"))
        fp_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(facility_plans)")).fetchall()}
        for col, typ in [
            ("interest_rate_annual", "FLOAT"),
            ("interest_method", "VARCHAR"),
            ("processing_fee_pct", "FLOAT"),
            ("principal_outstanding", "FLOAT"),
            ("future_interest_remaining", "FLOAT"),
        ]:
            if fp_cols and col not in fp_cols:
                conn.execute(text(f"ALTER TABLE facility_plans ADD COLUMN {col} {typ}"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
