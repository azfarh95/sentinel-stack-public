"""Compute principal-only outstanding + future interest remaining per instalment plan.

Different banks use different conventions:

  - **promo_zero** (or method missing on a 0%-promo plan): monthly × n = principal exact.
    DBS My Preferred Payment Plan (most of them), SC EZBAL-EASYPAY on promo,
    Maybank Flexicash 0% promos.

  - **flat** (declining-amount installment, constant interest each month):
    Singapore licensed moneylenders use this. Interest = principal × rate × years.
    Each month pays the same principal + same interest.

  - **reducing_balance** (standard amortization): interest each month = remaining
    balance × monthly rate. Principal portion grows over time. Bank loans typically
    use this for true term loans (e.g. DBS 003IL 60M 2.68%PA+1%PF).

For each plan we return:
  (principal_outstanding, future_interest_remaining)

Sum gives the YAML's stored `outstanding` (which is the future-payment-stream).
"""
from __future__ import annotations


def compute_principal_split(
    method: str,
    principal: float,
    monthly: float,
    original_months: int,
    remaining_months: int,
    interest_rate_annual: float = 0.0,
    processing_fee_pct: float = 0.0,
) -> tuple[float, float]:
    """Return (principal_outstanding, future_interest_remaining).

    None inputs are tolerated; missing data falls back to monthly × remaining
    (assumes promo_zero behavior, future_interest_remaining=0).
    """
    if not all([monthly, original_months, remaining_months is not None]):
        return (monthly * remaining_months if (monthly and remaining_months) else 0.0, 0.0)

    method = (method or "promo_zero").lower()

    if method == "promo_zero" or method == "none":
        principal_outstanding = monthly * remaining_months
        return (principal_outstanding, 0.0)

    if method == "flat":
        # Total cost over the loan = principal + flat_interest + fee
        # flat_interest = principal × (annual_rate/100) × years
        years = original_months / 12.0
        flat_interest = principal * (interest_rate_annual / 100.0) * years
        fee = principal * (processing_fee_pct / 100.0)
        # Principal paid per month is constant: principal / total_months
        principal_per_month = principal / original_months
        months_paid = original_months - remaining_months
        principal_outstanding = principal - (principal_per_month * months_paid)
        # Future interest + fee yet to be charged
        total_interest_plus_fee = flat_interest + fee
        unaccrued_per_month = total_interest_plus_fee / original_months
        future_interest_remaining = unaccrued_per_month * remaining_months
        return (principal_outstanding, future_interest_remaining)

    if method == "reducing_balance":
        # Solve for principal_outstanding given the monthly payment and remaining months.
        # PV = M × (1 - (1+r)^-n) / r
        r_monthly = (interest_rate_annual / 100.0) / 12.0
        if r_monthly <= 0:
            principal_outstanding = monthly * remaining_months
            future_interest_remaining = 0.0
        else:
            pv_factor = (1 - (1 + r_monthly) ** -remaining_months) / r_monthly
            principal_outstanding = monthly * pv_factor
            total_remaining = monthly * remaining_months
            future_interest_remaining = total_remaining - principal_outstanding
        return (principal_outstanding, future_interest_remaining)

    # Unknown method — be safe
    return (monthly * remaining_months, 0.0)
