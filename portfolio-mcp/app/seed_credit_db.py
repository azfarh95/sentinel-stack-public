"""Seed credit_facilities + payment_schedule from liabilities-registry.yaml.
Then match Firefly POSB withdrawals → actual_payments.

Run inside the portfolio-mcp container:
    docker exec portfolio-mcp python -m app.seed_credit_db

Idempotent: each facility is upserted by id. Schedule rows are wiped + rebuilt
per facility on every run. actual_payments are upserted by (facility_id, schedule_id)
so re-running doesn't duplicate matches.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import yaml
from sqlalchemy import delete, select

from . import amortization
from . import database as db

REGISTRY_PATH = Path(os.environ.get("LIABILITIES_REGISTRY", "/finance/liabilities-registry.yaml"))
FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
PAT = os.environ.get("FIREFLY_PAT", "")

MATCH_DAY_WINDOW = 5     # days before/after due_date
MATCH_AMOUNT_TOLERANCE = 1.00  # SGD


def _to_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)
        except Exception:
            return None
    return None


def upsert_facility(s, fac: dict) -> str:
    """Insert or update a credit_facilities row from a YAML facility dict.
    Returns the facility id."""
    fid = fac["id"]
    now = db.now_utc()
    existing = s.get(db.CreditFacility, fid)
    fields = {
        "firefly_acct_id": fac.get("firefly_acct_id"),
        "lender_name": fac.get("lender_name") or fac.get("name", "")[:64],
        "lender_uen": fac.get("lender_uen"),
        "lender_license": fac.get("lender_license"),
        "lender_address": fac.get("lender_address"),
        "lender_contact": fac.get("lender_contact"),
        "facility_type": fac.get("type", "unknown"),
        "account_number": fac.get("account_number"),
        "origination_date": _to_dt(fac.get("origination_date")),
        "maturity_date": _to_dt(fac.get("maturity_date")),
        "principal_amount": fac.get("principal_amount"),
        "disbursed_amount": fac.get("disbursed_amount"),
        "admin_fee": fac.get("admin_fee"),
        "nominal_monthly_pct": fac.get("nominal_monthly_pct"),
        "interest_basis": fac.get("interest_basis"),
        "eir_pct": fac.get("eir_pct"),
        "late_fee": fac.get("late_fee"),
        "statement_fee": fac.get("statement_fee"),
        "num_instalments": fac.get("num_instalments"),
        "instalment_amount": fac.get("instalment_amount"),
        "billing_day": fac.get("billing_day"),
        "status": fac.get("status", "active"),
        "credit_limit": fac.get("credit_limit"),
        "available_balance": fac.get("available"),
        "current_outstanding": fac.get("current_outstanding"),
        "agreement_document_path": fac.get("agreement_document_path"),
        "notes": fac.get("notes"),
        "shared_limit_with": fac.get("shared_limit_with"),
        "updated_at": now,
    }
    if existing is None:
        row = db.CreditFacility(id=fid, created_at=now, **fields)
        s.add(row)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
    return fid


def regenerate_plans(s, facility_id: str, plans: list[dict]) -> int:
    """Wipe + rebuild facility_plans for one facility. Returns row count.
    Also computes principal_outstanding + future_interest_remaining if interest
    fields are provided in YAML.
    """
    s.execute(delete(db.FacilityPlan).where(db.FacilityPlan.facility_id == facility_id))
    n = 0
    for p in plans:
        method = p.get("interest_method")
        principal_out = None
        future_int = None
        if method and p.get("kind") == "instalment" and p.get("monthly") and p.get("remaining_months") is not None:
            # Explicit interest model → compute principal-only via amortization
            principal_out, future_int = amortization.compute_principal_split(
                method=method,
                principal=float(p.get("principal") or 0),
                monthly=float(p.get("monthly") or 0),
                original_months=int(p.get("original_months") or 0),
                remaining_months=int(p.get("remaining_months") or 0),
                interest_rate_annual=float(p.get("interest_rate_annual") or 0),
                processing_fee_pct=float(p.get("processing_fee_pct") or 0),
            )
        else:
            # No explicit interest model → trust YAML.outstanding as-is.
            # This is the statement-balance for revolving plans + interest-bearing
            # plans where we haven't yet extracted the rate from the agreement.
            principal_out = p.get("outstanding")
            future_int = 0.0
        s.add(db.FacilityPlan(
            facility_id=facility_id,
            plan_id=p.get("id", ""),
            plan_code=p.get("plan_code"),
            kind=p.get("kind", "instalment"),
            principal=p.get("principal"),
            monthly=p.get("monthly"),
            original_months=p.get("original_months"),
            remaining_months=p.get("remaining_months"),
            outstanding=p.get("outstanding"),
            source=p.get("source"),
            interest_rate_annual=p.get("interest_rate_annual"),
            interest_method=method,
            processing_fee_pct=p.get("processing_fee_pct"),
            principal_outstanding=round(principal_out, 2) if principal_out is not None else None,
            future_interest_remaining=round(future_int, 2) if future_int is not None else None,
        ))
        n += 1
    return n


def regenerate_schedule(s, facility_id: str, schedule_rows: list[dict]) -> int:
    """Wipe + rebuild payment_schedule for one facility. Returns row count."""
    s.execute(delete(db.PaymentSchedule).where(db.PaymentSchedule.facility_id == facility_id))
    n = 0
    for row in schedule_rows:
        due = _to_dt(row.get("due_date"))
        s.add(db.PaymentSchedule(
            facility_id=facility_id,
            instalment_no=int(row["n"]),
            due_date=due,
            amount=float(row["amount"]),
            principal_portion=float(row.get("principal")) if row.get("principal") is not None else None,
            interest_portion=float(row.get("interest")) if row.get("interest") is not None else None,
            status="pending",
        ))
        n += 1
    return n


async def fetch_firefly_withdrawals(start: str, end: str) -> list[dict]:
    if not PAT:
        print("FIREFLY_PAT missing — skipping match phase", file=sys.stderr)
        return []
    out, page = [], 1
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            r = await c.get(f"{FIREFLY_URL}/api/v1/transactions",
                            headers={"Authorization": f"Bearer {PAT}", "Accept": "application/json"},
                            params={"start": start, "end": end, "type": "withdrawal",
                                    "limit": 200, "page": page})
            d = r.json()
            for t in d.get("data", []):
                a = t["attributes"]["transactions"][0]
                a["_id"] = int(t["id"])
                out.append(a)
            meta = d.get("meta", {}).get("pagination", {})
            if page >= int(meta.get("total_pages", 1) or 1):
                break
            page += 1
    return out


def match_payments(s, withdrawals: list[dict]) -> int:
    """For each pending schedule row, look for a Firefly withdrawal within
    ±MATCH_DAY_WINDOW days and ±MATCH_AMOUNT_TOLERANCE SGD.
    Upserts actual_payments rows (idempotent by (facility_id, schedule_id))."""
    now = db.now_utc()
    matched = 0
    schedules = s.execute(select(db.PaymentSchedule)).scalars().all()
    for sch in schedules:
        if sch.due_date is None:
            continue
        target_d = sch.due_date.date() if hasattr(sch.due_date, "date") else sch.due_date
        target_a = float(sch.amount)
        best = None
        best_dt_diff = 999
        for t in withdrawals:
            try:
                td = datetime.fromisoformat(t["date"][:10]).date()
            except Exception:
                continue
            day_diff = abs((td - target_d).days)
            amt = float(t.get("amount", 0))
            if day_diff <= MATCH_DAY_WINDOW and abs(amt - target_a) <= MATCH_AMOUNT_TOLERANCE:
                if day_diff < best_dt_diff:
                    best, best_dt_diff = t, day_diff
        if not best:
            continue
        # Upsert
        existing = s.execute(
            select(db.ActualPayment).where(
                db.ActualPayment.facility_id == sch.facility_id,
                db.ActualPayment.schedule_id == sch.id,
            )
        ).scalar_one_or_none()
        td = datetime.fromisoformat(best["date"][:10])
        fields = {
            "firefly_tx_id": int(best["_id"]),
            "paid_date": td,
            "amount": float(best.get("amount", 0)),
            "source_account": best.get("source_name") or "",
            "notes": (best.get("description") or "")[:200],
        }
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            s.add(db.ActualPayment(
                facility_id=sch.facility_id, schedule_id=sch.id, created_at=now, **fields
            ))
            matched += 1
        sch.status = "paid"
    return matched


async def main():
    print(f"[seed] reading {REGISTRY_PATH}")
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    facs = data.get("accounts", [])
    print(f"[seed] {len(facs)} facilities to seed")

    db.init_db()
    s = db.SessionLocal()
    sched_total = 0
    try:
        # Orphan purge: drop any DB facility no longer in YAML.
        # Cascade by hand: plans, schedule, actual_payments first.
        yaml_ids = {f["id"] for f in facs}
        all_ids = {row.id for row in s.execute(select(db.CreditFacility)).scalars().all()}
        orphans = all_ids - yaml_ids
        for oid in orphans:
            print(f"  [orphan] dropping facility '{oid}' (no longer in YAML)")
            s.execute(delete(db.ActualPayment).where(db.ActualPayment.facility_id == oid))
            s.execute(delete(db.PaymentSchedule).where(db.PaymentSchedule.facility_id == oid))
            s.execute(delete(db.FacilityPlan).where(db.FacilityPlan.facility_id == oid))
            s.execute(delete(db.CreditFacility).where(db.CreditFacility.id == oid))
        if orphans:
            s.commit()

        plans_total = 0
        for fac in facs:
            fid = upsert_facility(s, fac)
            # Plans (CC instalments, revolving min lines)
            np = regenerate_plans(s, fid, fac.get("plans", []))
            plans_total += np
            # Schedule (per-instalment P/I for fixed-term loans)
            if fac.get("schedule"):
                n = regenerate_schedule(s, fid, fac["schedule"])
                sched_total += n
                print(f"  [{fid}] facility + {np} plans + {n} schedule rows")
            else:
                s.execute(delete(db.PaymentSchedule).where(db.PaymentSchedule.facility_id == fid))
                print(f"  [{fid}] facility + {np} plans")
        s.commit()
        print(f"[seed] total: {len(facs)} facilities, {plans_total} plans, {sched_total} scheduled instalments")

        # Match phase
        print("\n[match] fetching Firefly withdrawals 2024-01-01 → today …")
        ws = await fetch_firefly_withdrawals("2024-01-01", date.today().isoformat())
        print(f"[match] {len(ws)} withdrawals to match against")
        matched = match_payments(s, ws)
        s.commit()
        print(f"[match] inserted {matched} new actual_payments rows")

        # Summary
        print("\n=== Summary ===")
        for fid in [f["id"] for f in facs]:
            fac = s.get(db.CreditFacility, fid)
            paid = s.execute(
                select(db.ActualPayment).where(db.ActualPayment.facility_id == fid)
            ).scalars().all()
            sched = s.execute(
                select(db.PaymentSchedule).where(db.PaymentSchedule.facility_id == fid)
            ).scalars().all()
            print(f"  {fid:<32} status={fac.status:<10} sched={len(sched):>2}  paid={len(paid):>2}")
    finally:
        s.close()


if __name__ == "__main__":
    asyncio.run(main())
