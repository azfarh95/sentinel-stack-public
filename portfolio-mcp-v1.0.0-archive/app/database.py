"""SQLite schema for portfolio snapshots. One row per (snapshot, token)."""
import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Index
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.environ.get("PORTFOLIO_DB", "/data/portfolio.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


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


def init_db():
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


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
