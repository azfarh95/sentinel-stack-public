"""Daily net-worth snapshot capture + retrieval.

Schedules:
  02:30 daily — capture_today()  → upserts one NetWorthSnapshot row keyed by date.
  Also runs once on lifespan startup if today's row is missing.

Used by v1.7 sparkline + future /networth-history full chart (planned).
"""
from __future__ import annotations

import logging
from datetime import datetime, date

from . import database as db

logger = logging.getLogger(__name__)


async def capture_today() -> dict:
    """Build today's snapshot from build_home_summary. Idempotent per date."""
    from . import home as home_mod
    summary = await home_mod.build_home_summary()
    today = date.today().isoformat()
    nw_sgd = float(summary["net_worth"]["sgd"])
    nw_usd = float(summary["net_worth"]["usd"])
    bank_sgd = float(summary["bank"]["sgd"])
    crypto_sgd = float(summary["crypto"]["sgd"])
    ilp_sgd = float(summary["ilp"]["sgd"])
    cpf_sgd = float(summary["cpf"]["sgd"])
    cc_sgd = float(summary["cc"]["sgd"])
    loans_sgd = float(summary["loans"]["sgd"])
    assets_sgd = bank_sgd + crypto_sgd + ilp_sgd + cpf_sgd
    liabilities_sgd = cc_sgd + loans_sgd
    fx = float(summary.get("fx", 1.27))

    s = db.SessionLocal()
    try:
        existing = (s.query(db.NetWorthSnapshot)
                     .filter(db.NetWorthSnapshot.snapshot_date == today).first())
        if existing:
            existing.captured_at = datetime.utcnow()
            existing.net_worth_sgd = nw_sgd
            existing.net_worth_usd = nw_usd
            existing.assets_sgd = assets_sgd
            existing.liabilities_sgd = liabilities_sgd
            existing.bank_sgd = bank_sgd
            existing.crypto_sgd = crypto_sgd
            existing.ilp_sgd = ilp_sgd
            existing.cpf_sgd = cpf_sgd
            existing.cc_sgd = cc_sgd
            existing.loans_sgd = loans_sgd
            existing.usd_to_sgd = fx
            row = existing
            mode = "updated"
        else:
            row = db.NetWorthSnapshot(
                captured_at=datetime.utcnow(),
                snapshot_date=today,
                net_worth_sgd=nw_sgd, net_worth_usd=nw_usd,
                assets_sgd=assets_sgd, liabilities_sgd=liabilities_sgd,
                bank_sgd=bank_sgd, crypto_sgd=crypto_sgd,
                ilp_sgd=ilp_sgd, cpf_sgd=cpf_sgd,
                cc_sgd=cc_sgd, loans_sgd=loans_sgd,
                usd_to_sgd=fx,
            )
            s.add(row)
            mode = "created"
        s.commit()
        return {"mode": mode, "date": today, "net_worth_sgd": nw_sgd}
    finally:
        s.close()


def load_history(limit: int = 90) -> list[dict]:
    s = db.SessionLocal()
    try:
        rows = (s.query(db.NetWorthSnapshot)
                 .order_by(db.NetWorthSnapshot.snapshot_date.asc())
                 .limit(limit).all())
        return [{
            "date": r.snapshot_date,
            "net_worth_sgd": r.net_worth_sgd,
            "net_worth_usd": r.net_worth_usd,
            "assets_sgd": r.assets_sgd,
            "liabilities_sgd": r.liabilities_sgd,
        } for r in rows]
    finally:
        s.close()
