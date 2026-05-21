"""SQLite-backed state. Two tables:
  - shopify_stores  : the registry of Shopify domains the adapter searches
  - price_history   : every listing returned by any adapter, for trend queries

Indexes designed for the bot's likely questions:
  - "is this normally this price?"   -> by (marketplace, url, captured_at)
  - "what's cheapest right now?"     -> always live; history is the secondary lookup
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from schema import Listing, ShopifyStore, TelcoPlan

def _db_path() -> Path:
    if Path("/.dockerenv").exists() and Path("/data").exists():
        return Path("/data/sentinel-shopping.db")
    return Path(__file__).parent / "sentinel-shopping.db"


DB_PATH = _db_path()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shopify_stores (
    domain        TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'SGD',
    enabled       INTEGER NOT NULL DEFAULT 1,
    added_at      TEXT NOT NULL,
    last_seen_at  TEXT,
    notes         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_history (
    id            INTEGER PRIMARY KEY,
    marketplace   TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    price_sgd     REAL,
    discount_pct  REAL,
    rating        REAL,
    image_url     TEXT,
    in_stock      INTEGER,
    vendor        TEXT,
    query         TEXT NOT NULL,        -- search query that surfaced this row
    captured_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ph_marketplace_url     ON price_history(marketplace, url);
CREATE INDEX IF NOT EXISTS ix_ph_query_captured      ON price_history(query, captured_at);
CREATE INDEX IF NOT EXISTS ix_ph_captured            ON price_history(captured_at);

CREATE TABLE IF NOT EXISTS telco_plans_history (
    id                     INTEGER PRIMARY KEY,
    carrier                TEXT NOT NULL,
    network                TEXT NOT NULL,
    category               TEXT NOT NULL,
    plan_name              TEXT NOT NULL,
    monthly_sgd            REAL,
    monthly_sgd_steady     REAL,
    promo_months           INTEGER,
    contract_months        INTEGER,
    data_gb                REAL,
    speed_mbps             INTEGER,
    free_addons            TEXT,
    roaming_note           TEXT,
    url                    TEXT,
    cis_pdf_url            TEXT,
    platform_fee_included  INTEGER NOT NULL DEFAULT 0,
    captured_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tph_carrier_captured ON telco_plans_history(carrier, captured_at);
CREATE INDEX IF NOT EXISTS ix_tph_category_price   ON telco_plans_history(category, monthly_sgd);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA_SQL)


# ── Shopify-stores registry ─────────────────────────────────────────────────

def add_shopify_store(store: ShopifyStore) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO shopify_stores (domain, display_name, currency, enabled, added_at, last_seen_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                display_name = excluded.display_name,
                currency     = excluded.currency,
                enabled      = excluded.enabled,
                notes        = excluded.notes
        """, (store.domain, store.display_name, store.currency,
              1 if store.enabled else 0, store.added_at, store.last_seen_at, store.notes))


def list_shopify_stores(enabled_only: bool = True) -> list[ShopifyStore]:
    sql = "SELECT * FROM shopify_stores"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY domain"
    with conn() as c:
        rows = c.execute(sql).fetchall()
    return [ShopifyStore(
        domain=r["domain"], display_name=r["display_name"], currency=r["currency"],
        enabled=bool(r["enabled"]), added_at=r["added_at"], last_seen_at=r["last_seen_at"],
        notes=r["notes"] or "",
    ) for r in rows]


def remove_shopify_store(domain: str) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM shopify_stores WHERE domain = ?", (domain,))
        return cur.rowcount > 0


def touch_shopify_store(domain: str, when_iso: str) -> None:
    with conn() as c:
        c.execute("UPDATE shopify_stores SET last_seen_at = ? WHERE domain = ?", (when_iso, domain))


# ── Price history ──────────────────────────────────────────────────────────

def record_listings(query: str, listings: Iterable[Listing]) -> int:
    rows = []
    for l in listings:
        rows.append((
            l.marketplace, l.title, l.url, l.price_sgd, l.discount_pct, l.rating,
            l.image_url, 1 if l.in_stock else (0 if l.in_stock is False else None),
            l.vendor, query, l.captured_at,
        ))
    if not rows:
        return 0
    with conn() as c:
        c.executemany("""
            INSERT INTO price_history
                (marketplace, title, url, price_sgd, discount_pct, rating,
                 image_url, in_stock, vendor, query, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
    return len(rows)


def record_telco_plans(plans: Iterable[TelcoPlan]) -> int:
    import json as _json
    rows = []
    for p in plans:
        # float('inf') -> store as None (SQLite has no inf)
        data_gb = p.data_gb
        if data_gb is not None and data_gb == float("inf"):
            data_gb = None
        rows.append((
            p.carrier, p.network, p.category, p.plan_name,
            p.monthly_sgd, p.monthly_sgd_steady, p.promo_months, p.contract_months,
            data_gb, p.speed_mbps, _json.dumps(p.free_addons or []),
            p.roaming_note, p.url, p.cis_pdf_url,
            1 if p.platform_fee_included else 0, p.captured_at,
        ))
    if not rows:
        return 0
    with conn() as c:
        c.executemany("""
            INSERT INTO telco_plans_history
                (carrier, network, category, plan_name,
                 monthly_sgd, monthly_sgd_steady, promo_months, contract_months,
                 data_gb, speed_mbps, free_addons, roaming_note, url, cis_pdf_url,
                 platform_fee_included, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
    return len(rows)


def history_for_url(url: str, days: int = 30) -> list[dict]:
    """Recent price points for the same listing URL."""
    with conn() as c:
        rows = c.execute("""
            SELECT captured_at, price_sgd, discount_pct, in_stock
            FROM price_history
            WHERE url = ?
              AND captured_at >= datetime('now', ?)
            ORDER BY captured_at
        """, (url, f'-{int(days)} days')).fetchall()
    return [dict(r) for r in rows]
