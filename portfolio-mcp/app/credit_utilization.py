"""Credit utilization + reconciliation for credit facilities.

A+B=C+D check, per the owner's accounting identity:
    credit_limit = available_balance + current_outstanding
    current_outstanding = Σ(plan.outstanding) + revolving_balance

Any non-zero delta = data error (paid-off plan still tracked, stale
statement balance, double-counted plan, etc.). Surfaced as alerts.
"""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select, func

from . import database as db


# Facility types that genuinely have NO credit_limit (term loans, moneylenders).
# For these, A+B=C+D simplifies to: current_outstanding == Σ(plan.outstanding).
_NON_REVOLVING = {"term_loan", "moneylender_loan", "balance_transfer", "digital_loan"}


@dataclass
class FacilityUtilization:
    id: str
    name: str
    facility_type: str
    status: str
    credit_limit: float | None
    available: float | None
    outstanding: float
    plans_sum: float
    revolving: float          # outstanding - plans_sum
    util_pct: float | None    # outstanding / credit_limit  (None if no limit)
    delta_limit: float | None # credit_limit - (available + outstanding)
    delta_plans: float        # outstanding - (plans_sum + revolving)  → always 0 by definition
    reconciled: bool          # True if both deltas within tolerance
    note: str                 # human-readable reconciliation summary

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "facility_type": self.facility_type,
            "status": self.status,
            "credit_limit": self.credit_limit, "available": self.available,
            "outstanding": self.outstanding, "plans_sum": round(self.plans_sum, 2),
            "revolving": round(self.revolving, 2),
            "util_pct": round(self.util_pct, 2) if self.util_pct is not None else None,
            "delta_limit": round(self.delta_limit, 2) if self.delta_limit is not None else None,
            "reconciled": self.reconciled, "note": self.note,
        }


def compute_utilization(s=None, tolerance: float = 0.50) -> tuple[list[FacilityUtilization], dict]:
    """Return (per-facility list, aggregate dict).

    Reconciliation handles shared_limit_with: when facility B links to A,
    A's limit check includes B's outstanding too. B's own limit check is skipped
    (B inherits from A).
    """
    own = s is None
    if own:
        s = db.SessionLocal()
    try:
        facs = s.execute(select(db.CreditFacility)).scalars().all()
        facs_by_id = {f.id: f for f in facs}
        # Use principal_outstanding when available (interest-bearing plans),
        # else fall back to outstanding (0% promo plans, statement-balance plans).
        plans = s.execute(select(db.FacilityPlan)).scalars().all()
        plan_sums: dict[str, float] = {}
        for p in plans:
            val = p.principal_outstanding if p.principal_outstanding is not None else (p.outstanding or 0)
            plan_sums[p.facility_id] = plan_sums.get(p.facility_id, 0.0) + (val or 0)

        # Linked-outstanding lookup: for each parent, sum outstandings of all linked children.
        linked_children: dict[str, list[str]] = {}
        for f in facs:
            if f.shared_limit_with:
                linked_children.setdefault(f.shared_limit_with, []).append(f.id)

        out: list[FacilityUtilization] = []
        agg_limit = 0.0
        agg_used = 0.0
        agg_avail = 0.0
        any_unreconciled = False

        for f in facs:
            cl = float(f.credit_limit) if f.credit_limit is not None else None
            av = float(f.available_balance) if f.available_balance is not None else None
            os_ = float(f.current_outstanding or 0)
            ps = float(plan_sums.get(f.id, 0) or 0)
            revolving = os_ - ps
            util = (os_ / cl * 100.0) if (cl and cl > 0) else None

            # Linked outstanding: if this facility holds the limit for others,
            # sum their outstandings into the limit-side check.
            linked_outstanding = 0.0
            linked_ids = linked_children.get(f.id, [])
            for child_id in linked_ids:
                child = facs_by_id.get(child_id)
                if child:
                    linked_outstanding += float(child.current_outstanding or 0)

            # A+B=C+D check
            #   limit (A)  =  available (C)  +  outstanding (D)  +  linked children's outstanding
            if cl is not None and av is not None:
                delta_limit = cl - (av + os_ + linked_outstanding)
            else:
                delta_limit = None

            # If this facility is a CHILD (links to a parent), skip the limit-delta
            # check — it doesn't own a limit.
            if f.shared_limit_with:
                delta_limit = None

            # Plan-side check: for fixed-term facilities, plans should sum to outstanding
            # (revolving = ~0). For CCs, revolving may be positive (the non-plan balance).
            # We flag plan_sum > outstanding (over-counted plans) or plans_sum > 0 on
            # a paid_off facility.
            note_parts = []
            ok = True
            if delta_limit is not None and abs(delta_limit) > tolerance:
                ok = False
                note_parts.append(f"limit_delta={delta_limit:+.2f}")
            if f.facility_type in _NON_REVOLVING:
                # Should be exactly: outstanding = Σ plans (no revolving on non-revolving facility)
                if abs(revolving) > tolerance:
                    ok = False
                    note_parts.append(f"non_revolving_residual={revolving:+.2f}")
            else:
                # CC / line of credit: plans <= outstanding (plans can't exceed it)
                if ps - os_ > tolerance:
                    ok = False
                    note_parts.append(f"plans_overshoot={ps - os_:+.2f}")
                # Paid-off facility shouldn't have plans
                if f.status == "paid_off" and ps > tolerance:
                    ok = False
                    note_parts.append(f"paid_off_but_plans={ps:.2f}")
            if ok and not note_parts:
                note_parts.append("✓ reconciled")

            # Append linked-children context to note for clarity
            if linked_ids:
                note_parts.append(f"limit shared with: {', '.join(linked_ids)}")
            if f.shared_limit_with:
                note_parts.append(f"linked to {f.shared_limit_with}")

            row = FacilityUtilization(
                id=f.id, name=f.lender_name or f.id,
                facility_type=f.facility_type, status=f.status,
                credit_limit=cl, available=av, outstanding=os_,
                plans_sum=ps, revolving=revolving,
                util_pct=util,
                delta_limit=delta_limit,
                delta_plans=0.0,
                reconciled=ok,
                note=" · ".join(note_parts),
            )
            out.append(row)
            if not ok:
                any_unreconciled = True

            # Aggregate (skip facilities with no limit — term loans)
            if cl is not None:
                agg_limit += cl
            if av is not None:
                agg_avail += av
            agg_used += os_

        # Aggregate util only counts facilities with limits
        revolving_limit_used = sum(
            r.outstanding for r in out if r.credit_limit is not None
        )
        agg_util = (revolving_limit_used / agg_limit * 100.0) if agg_limit > 0 else None

        return out, {
            "total_credit_limit": round(agg_limit, 2),
            "total_used": round(agg_used, 2),
            "total_available": round(agg_avail, 2),
            "total_used_against_limit": round(revolving_limit_used, 2),
            "aggregate_util_pct": round(agg_util, 2) if agg_util is not None else None,
            "facility_count": len(out),
            "unreconciled_count": sum(1 for r in out if not r.reconciled),
            "any_unreconciled": any_unreconciled,
        }
    finally:
        if own:
            s.close()


def render_html(rows: list[FacilityUtilization], agg: dict) -> str:
    """Render the /admin/credit_utilization page."""
    def fmt(v):
        return f"${v:,.2f}" if v is not None else "—"

    def util_pill(util):
        if util is None:
            return "—"
        if util >= 90:
            color = "#ff3b30"
        elif util >= 70:
            color = "#ff9500"
        else:
            color = "#4cd964"
        return f'<span style="color:{color};font-weight:600">{util:.0f}%</span>'

    def status_pill(s):
        c = {"active": "#4cd964", "paid_off": "#8e8e93",
             "in_default": "#ff3b30", "restructured": "#ff9500",
             "cancelled": "#8e8e93"}.get(s, "#8e8e93")
        return f'<span style="color:{c};font-size:11px;text-transform:uppercase;letter-spacing:.5px">{s}</span>'

    alerts = [r for r in rows if not r.reconciled]
    alerts_html = ""
    if alerts:
        items = "".join(
            f'<li><b>{r.name}</b> <span style="color:#8e8e93">({r.id})</span>: {r.note}</li>'
            for r in alerts
        )
        alerts_html = (
            '<div style="background:rgba(255,59,48,0.1);border:1px solid rgba(255,59,48,0.4);'
            'border-radius:8px;padding:14px;margin-bottom:18px;">'
            '<h3 style="margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:#ff3b30">'
            f'⚠ Reconciliation Alerts ({len(alerts)})</h3>'
            f'<ul style="margin:6px 0 0 18px;padding:0;font-size:13px;line-height:1.6">{items}</ul>'
            '</div>'
        )
    else:
        alerts_html = (
            '<div style="background:rgba(76,217,100,0.08);border:1px solid rgba(76,217,100,0.3);'
            'border-radius:8px;padding:10px 14px;margin-bottom:18px;color:#4cd964;font-size:13px">'
            '✓ All facilities reconcile (A+B=C+D holds across the portfolio)</div>'
        )

    rows_html = []
    for r in rows:
        cls = "" if r.reconciled else "background:rgba(255,59,48,0.05)"
        rows_html.append(
            f'<tr style="{cls}">'
            f'<td><b>{r.name}</b><br><span style="color:#8e8e93;font-size:11px">{r.id} · {r.facility_type}</span></td>'
            f'<td style="text-align:right">{status_pill(r.status)}</td>'
            f'<td style="text-align:right">{fmt(r.credit_limit)}</td>'
            f'<td style="text-align:right">{fmt(r.outstanding)}</td>'
            f'<td style="text-align:right">{fmt(r.available)}</td>'
            f'<td style="text-align:right">{util_pill(r.util_pct)}</td>'
            f'<td style="text-align:right">{fmt(r.plans_sum)}</td>'
            f'<td style="text-align:right">{fmt(r.revolving)}</td>'
            f'<td style="text-align:right;color:{("#ff3b30" if not r.reconciled else "#8e8e93")};'
            'font-size:11px">{}</td>'.format(r.note)
            + "</tr>"
        )

    css = """
    body { background:#1c1c1e; color:#f0f0f0; font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;
           margin:0; padding:18px 14px 60px; max-width:1000px; margin:0 auto; }
    h1 { font-size:22px; margin:0 0 4px; }
    .meta { color:#8e8e93; font-size:12px; margin-bottom:18px; }
    .back { color:#4cd964; font-size:13px; text-decoration:none; display:inline-block; margin-bottom:10px; }
    .summary { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:18px; }
    .summary .card { background:#2c2c2e; border-radius:10px; padding:12px 14px; }
    .summary .label { color:#8e8e93; font-size:10px; text-transform:uppercase; letter-spacing:.6px; }
    .summary .value { font-size:18px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th { text-align:left; padding:8px 6px; color:#8e8e93; font-size:10px; text-transform:uppercase;
         letter-spacing:.6px; border-bottom:1px solid rgba(255,255,255,.1); }
    th.right { text-align:right; }
    td { padding:10px 6px; border-bottom:1px solid rgba(255,255,255,.05);
         font-variant-numeric:tabular-nums; }
    """

    body = f"""
    <a class="back" href="/">&larr; Home</a>
    <h1>Credit Utilization</h1>
    <div class="meta">A+B=C+D reconciliation across all credit facilities · base SGD</div>
    {alerts_html}
    <div class="summary">
      <div class="card"><div class="label">Total Credit Limit</div><div class="value">{fmt(agg['total_credit_limit'])}</div></div>
      <div class="card"><div class="label">Total Used</div><div class="value">{fmt(agg['total_used'])}</div></div>
      <div class="card"><div class="label">Total Available</div><div class="value">{fmt(agg['total_available'])}</div></div>
      <div class="card"><div class="label">Aggregate Util</div><div class="value">{util_pill(agg['aggregate_util_pct'])}</div></div>
    </div>
    <table>
      <thead><tr>
        <th>Facility</th>
        <th class="right">Status</th>
        <th class="right">Limit</th>
        <th class="right">Outstanding</th>
        <th class="right">Available</th>
        <th class="right">Util %</th>
        <th class="right">Plans Σ</th>
        <th class="right">Revolving</th>
        <th class="right">Reconciliation</th>
      </tr></thead>
      <tbody>{"".join(rows_html)}</tbody>
    </table>
    """
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Credit Utilization — Sentinel Finance</title>'
        '<link rel="manifest" href="/manifest.webmanifest">'
        f'<style>{css}</style>'
        '</head><body>' + body + '</body></html>'
    )
