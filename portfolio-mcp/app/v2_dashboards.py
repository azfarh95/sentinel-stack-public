"""V2 dashboard routes — surfaces the canonical registries + unreconciled queue.

Routes:
  GET  /reconcile             unreconciled queue triage (pending items)
  POST /reconcile/{id}/post   approve queued candidate → post to GL
  POST /reconcile/{id}/reject mark queued candidate rejected (no GL post)
  GET  /facilities            CreditFacility list (canonical owner for liabilities)
  GET  /policies              InsurancePolicyRegistry + latest ILP snapshots

All routes share the auth/look pattern of coa_view.py.
"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select, text

from . import database as db
from . import ledger
from . import journal_service


_CSS = """
body { background:#1c1c1e; color:#f0f0f0; font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;
       margin:0; padding:18px 14px 60px; max-width:1100px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; }
.meta { color:#8e8e93; font-size:12px; margin-bottom:18px; }
.back { color:#4cd964; font-size:13px; text-decoration:none; display:inline-block; margin-bottom:10px; }
.card { background:#2c2c2e; border-radius:10px; padding:12px 14px; margin:8px 0; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #3a3a3c; }
th { color:#8e8e93; font-weight:500; font-size:11px; text-transform:uppercase; }
tr:hover { background:#262628; }
.amt-out { color:#ff6b6b; text-align:right; font-variant-numeric:tabular-nums; }
.amt-in  { color:#4cd964; text-align:right; font-variant-numeric:tabular-nums; }
.amt     { text-align:right; font-variant-numeric:tabular-nums; }
.pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
.pill-active   { background:#1f4d2b; color:#4cd964; }
.pill-pending  { background:#4d3e1f; color:#ffcc00; }
.pill-rejected { background:#4d1f1f; color:#ff6b6b; }
.pill-resolved { background:#1f3e4d; color:#5ac8fa; }
.pill-lapsed   { background:#3a3a3c; color:#8e8e93; }
.pill-t1       { background:#3a2d4d; color:#bf5af2; }
.pill-t2       { background:#4d1f1f; color:#ff6b6b; }
.pill-t3       { background:#4d3e1f; color:#ffcc00; }
.conf { color:#8e8e93; font-size:11px; }
button { background:#4cd964; border:none; color:#000; padding:6px 12px; border-radius:6px; font-weight:600;
         font-size:12px; cursor:pointer; margin-right:4px; }
button.reject { background:#ff6b6b; color:#000; }
input.coa-edit { background:#1c1c1e; color:#f0f0f0; border:1px solid #3a3a3c; padding:4px 6px;
                 border-radius:4px; width:72px; font-size:12px; }
.legend { color:#8e8e93; font-size:12px; margin-bottom:12px; }
details { background:#262628; border-radius:8px; padding:6px 10px; margin:4px 0; font-size:12px; }
details summary { cursor:pointer; color:#8e8e93; }
"""


def _layout(title: str, meta: str, body_inner: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Sentinel Finance</title>
<style>{_CSS}</style></head><body>
<a class="back" href="/">&larr; Home</a>
<h1>{title}</h1>
<div class="meta">{meta}</div>
{body_inner}
</body></html>"""


# ──────────────────────────────────────────────────────────────────────────────
# /reconcile
# ──────────────────────────────────────────────────────────────────────────────


def _render_match(m: dict) -> str:
    """Format one best-guess Match dict as a compact line."""
    src = m.get("source", "?")
    coa = m.get("contra_coa", "")
    reason = m.get("reason", "")
    conf = m.get("confidence", 0)
    return f'<div class="conf">↳ {src} → CoA {coa} <i>({reason})</i> <b>{conf}%</b></div>'


def _render_queue_row(row) -> str:
    rid = row.id
    d = row.tx_date.isoformat() if hasattr(row.tx_date, "isoformat") else str(row.tx_date)
    amt = float(row.tx_amount)
    direction = (row.direction or "").upper()
    amt_cls = "amt-out" if direction == "OUT" else "amt-in" if direction == "IN" else "amt"
    narr = (row.tx_narration or "")[:80]
    tx_type = row.tx_type or ""
    src = row.source_doc or ""

    best = json.loads(row.best_guess_matches or "[]")
    top_coa = best[0]["contra_coa"] if best else ""

    matches_html = "".join(_render_match(m) for m in best[:3]) or '<div class="conf">no matches</div>'

    cj = json.loads(row.candidate_journal or "[]")
    cj_html = "<br>".join(
        f"&nbsp;&nbsp;{l.get('account_code', '?'):<6} "
        f"Dr {l.get('debit', 0):>8} Cr {l.get('credit', 0):>8}"
        for l in cj
    )

    return f"""
    <tr>
      <td>{d}</td>
      <td>{src}<br><span class="conf">{tx_type}</span></td>
      <td class="{amt_cls}">{amt:,.2f}</td>
      <td>{narr}</td>
      <td>
        {matches_html}
        <details><summary>proposed journal</summary><div class="conf">{cj_html}</div></details>
      </td>
      <td>
        <form method="post" action="/reconcile/{rid}/post" style="display:inline;">
          <input class="coa-edit" name="contra_coa" value="{top_coa}" placeholder="CoA">
          <button type="submit">Post</button>
        </form>
        <form method="post" action="/reconcile/{rid}/reject" style="display:inline;">
          <button type="submit" class="reject">Skip</button>
        </form>
      </td>
    </tr>
    """


_CLASS_LABELS = {
    "pre_opening": ("T1", "pill-t1", "pre-opening → 3100 Retained Earnings"),
    "unresolvable": ("T2", "pill-t2", "unresolvable → 5990 Reconciliation Adj"),
    "fixable": ("T3", "pill-t3", "fixable → wait for counter-statement"),
}


# Pass-7 Q1: per-row triage categories. user_decision stores 'noise:<cat>'.
DRIFT_TRIAGE_CATEGORIES = [
    ("peer_paynow",     "Peer PayNow"),
    ("cash_withdrawal", "Cash withdrawal"),
    ("legacy_gap",      "Legacy gap"),
    ("other",           "Other / noise"),
]


def _render_drift_row(s, row) -> str:
    """One PERIOD_DRIFT row with classification badge (T1/T2/T3) and triage form."""
    from . import journal_service as _js
    cls = _js.classify_drift(s, {
        "source_ref": row.source_ref,
        "tx_date": row.tx_date,
    })
    tag, pill_cls, hint = _CLASS_LABELS.get(cls, ("?", "pill-lapsed", cls))
    rid = row.id
    d = row.tx_date.isoformat() if hasattr(row.tx_date, "isoformat") else str(row.tx_date)
    amt = float(row.tx_amount)
    direction = (row.direction or "").upper()
    amt_cls = "amt-out" if direction == "OUT" else "amt-in" if direction == "IN" else "amt"
    narr = (row.tx_narration or "")[:140]
    ref = row.source_ref or ""

    options = "".join(f'<option value="{c}">{lbl}</option>'
                       for c, lbl in DRIFT_TRIAGE_CATEGORIES)
    return f"""
    <tr>
      <td>{d}</td>
      <td><code class="conf">{ref}</code></td>
      <td class="{amt_cls}">{amt:,.2f}</td>
      <td>{narr}</td>
      <td><span class="pill {pill_cls}">{tag}</span>
          <span class="conf">&nbsp;{hint}</span></td>
      <td>
        <form method="post" action="/reconcile/{rid}/triage" style="display:inline;">
          <select class="coa-edit" name="category" style="width:auto;">{options}</select>
          <button type="submit" title="Tag as confirmed noise and remove from queue">Triage</button>
        </form>
        <form method="post" action="/reconcile/{rid}/reject" style="display:inline;">
          <button type="submit" class="reject" title="Skip without categorising">Skip</button>
        </form>
      </td>
    </tr>
    """


def render_reconcile_page() -> str:
    s = db.SessionLocal()
    try:
        pending = s.execute(
            select(ledger.UnreconciledQueue)
            .where(ledger.UnreconciledQueue.status == "pending")
            .order_by(ledger.UnreconciledQueue.tx_date.desc(), ledger.UnreconciledQueue.id.desc())
            .limit(500)
        ).scalars().all()

        drift_rows = [r for r in pending if (r.tx_type or "").upper() == "PERIOD_DRIFT"]
        verifier_rows = [r for r in pending if (r.tx_type or "").upper() != "PERIOD_DRIFT"]

        total_pending = s.execute(text(
            "SELECT COUNT(*) FROM unreconciled_queue WHERE status='pending'"
        )).scalar() or 0
        total_resolved = s.execute(text(
            "SELECT COUNT(*) FROM unreconciled_queue WHERE status='resolved'"
        )).scalar() or 0
        total_rejected = s.execute(text(
            "SELECT COUNT(*) FROM unreconciled_queue WHERE status='rejected'"
        )).scalar() or 0

        # Tally drift classifications for the legend
        drift_tally = {"pre_opening": 0, "unresolvable": 0, "fixable": 0}
        drift_rows_html = []
        for r in drift_rows:
            drift_rows_html.append(_render_drift_row(s, r))
            from . import journal_service as _js
            cls = _js.classify_drift(s, {
                "source_ref": r.source_ref, "tx_date": r.tx_date,
            })
            drift_tally[cls] = drift_tally.get(cls, 0) + 1
    finally:
        s.close()

    verifier_html = "".join(_render_queue_row(r) for r in verifier_rows) or \
        '<tr><td colspan="6" style="text-align:center;color:#8e8e93;padding:20px;">Queue is empty.</td></tr>'
    drift_html = "".join(drift_rows_html) or \
        '<tr><td colspan="6" style="text-align:center;color:#8e8e93;padding:20px;">No period drift.</td></tr>'

    drift_card = f"""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div><b>Period drift</b>
             <span class="conf">&nbsp; {len(drift_rows)} items · Gate 4 mismatches</span></div>
        <div>
          <span class="pill pill-t1">T1 {drift_tally['pre_opening']}</span>
          <span class="pill pill-t2">T2 {drift_tally['unresolvable']}</span>
          <span class="pill pill-t3">T3 {drift_tally['fixable']}</span>
        </div>
      </div>
      <div class="legend">
        Drift between statement CF and our GL projection. Classification (audit-4):
        <b>T1</b> = before opening anchor → 3100; <b>T2</b> = no counter-coverage → 5990;
        <b>T3</b> = counter-statement overlaps drift period → wait for more data.
      </div>
      <table>
        <thead><tr>
          <th>Date</th><th>Account:Period</th><th>Amount</th>
          <th>Narration</th><th>Classification</th><th>Action</th>
        </tr></thead>
        <tbody>{drift_html}</tbody>
      </table>
    </div>
    """

    # v2.26: prominent link to Suspense reclassifier if there's work to do.
    try:
        from . import suspense_reclassifier as _sr
        with db.session_scope() as _s:
            _susp = _sr.scan(_s)
        n_high = len(_susp.get("HIGH", []))
        n_med = len(_susp.get("MED", []))
        n_low = len(_susp.get("LOW", []))
        susp_total = sum(abs(p.amount) for bucket in _susp.values() for p in bucket)
    except Exception:
        n_high = n_med = n_low = 0
        susp_total = 0.0

    suspense_banner = ""
    if n_high + n_med + n_low > 0:
        suspense_banner = f"""
        <a href="/reconcile/suspense" style="text-decoration:none;color:inherit;">
        <div class="card" style="background:#1f3e4d;border:1px solid #5ac8fa;cursor:pointer;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <b style="color:#5ac8fa;">🛠 Suspense reclassifier (v2.26)</b>
              <div class="conf" style="margin-top:4px;">
                {n_high} HIGH · {n_med} MED · {n_low} LOW
                · ${susp_total:,.2f} of journals routed to 1190 Suspense
              </div>
            </div>
            <div style="font-size:20px;color:#5ac8fa;">→</div>
          </div>
        </div>
        </a>"""

    body = f"""
    <div class="legend">
      Items the pre-posting verifier couldn't auto-classify (confidence &lt; 75).
      Edit the CoA if needed, then <b>Post</b> to write to GL, or <b>Skip</b> to ignore.
    </div>
    {suspense_banner}
    <div class="card" style="display:flex;gap:24px;">
      <div><b style="color:#ffcc00;">{total_pending}</b> <span class="conf">pending</span></div>
      <div><b style="color:#5ac8fa;">{total_resolved}</b> <span class="conf">resolved</span></div>
      <div><b style="color:#ff6b6b;">{total_rejected}</b> <span class="conf">rejected</span></div>
    </div>
    {drift_card}
    <div class="card">
      <table>
        <thead><tr>
          <th>Date</th><th>Source</th><th>Amount</th><th>Narration</th>
          <th>Best guess</th><th>Action</th>
        </tr></thead>
        <tbody>{verifier_html}</tbody>
      </table>
    </div>
    """
    return _layout("Unreconciled Queue",
                   f"{total_pending} pending ({len(drift_rows)} drift + {len(verifier_rows)} verifier)"
                   f" · resolves write to GL + back to registries",
                   body)


def resolve_post(queue_id: int, user_contra_coa: str | None) -> tuple[bool, str]:
    """Post a queued candidate to GL. Returns (ok, message)."""
    s = db.SessionLocal()
    try:
        row = s.get(ledger.UnreconciledQueue, queue_id)
        if row is None:
            return False, f"queue id {queue_id} not found"
        if row.status != "pending":
            return False, f"already {row.status}"

        lines = json.loads(row.candidate_journal or "[]")
        if not lines:
            return False, "no candidate journal lines"

        # If user supplied a contra_coa, swap the placeholder leg.
        if user_contra_coa:
            placeholder_codes = {"1190", "5910", "9999"}  # suspense / unknown buckets
            for ln in lines:
                if ln.get("account_code") in placeholder_codes:
                    ln["account_code"] = user_contra_coa
                    break

        tx_date = row.tx_date if isinstance(row.tx_date, date) \
            else date.fromisoformat(str(row.tx_date))

        jid = journal_service.post_journal(
            s,
            journal_date=tx_date,
            narration=(row.tx_narration or "queued resolution")[:200],
            journal_type="verifier_resolution",
            lines=lines,
            source_doc=row.source_doc,
            source_ref=row.source_ref,
            external_id=f"queue:{row.id}",
            created_by="user",
        )

        row.status = "resolved"
        row.user_decision = user_contra_coa or "as_proposed"
        row.posted_journal_id = jid
        row.resolved_at = db.now_utc()
        s.commit()
        return True, f"posted journal #{jid}"
    except Exception as e:
        s.rollback()
        return False, f"error: {e!s}"
    finally:
        s.close()


def resolve_reject(queue_id: int) -> tuple[bool, str]:
    s = db.SessionLocal()
    try:
        row = s.get(ledger.UnreconciledQueue, queue_id)
        if row is None:
            return False, f"queue id {queue_id} not found"
        if row.status != "pending":
            return False, f"already {row.status}"
        row.status = "rejected"
        row.user_decision = "skip"
        row.resolved_at = db.now_utc()
        s.commit()
        return True, "rejected"
    except Exception as e:
        s.rollback()
        return False, f"error: {e!s}"
    finally:
        s.close()


def resolve_triage(queue_id: int, category: str) -> tuple[bool, str]:
    """Pass-7 Q1: tag a PERIOD_DRIFT row as confirmed noise.
    status='triaged', user_decision='noise:<category>'. Never writes a journal.
    """
    valid = {c for c, _ in DRIFT_TRIAGE_CATEGORIES}
    if category not in valid:
        return False, f"unknown category {category!r}; valid: {sorted(valid)}"
    s = db.SessionLocal()
    try:
        row = s.get(ledger.UnreconciledQueue, queue_id)
        if row is None:
            return False, f"queue id {queue_id} not found"
        if row.status != "pending":
            return False, f"already {row.status}"
        row.status = "triaged"
        row.user_decision = f"noise:{category}"
        row.resolved_at = db.now_utc()
        s.commit()
        return True, f"triaged as noise:{category}"
    except Exception as e:
        s.rollback()
        return False, f"error: {e!s}"
    finally:
        s.close()


# ──────────────────────────────────────────────────────────────────────────────
# /reconcile/suspense (v2.26 — pass-11 Q1 hybrid bulk reclassifier)
# ──────────────────────────────────────────────────────────────────────────────


def render_suspense_page() -> str:
    """Render 3 buckets (HIGH/MED/LOW) of Suspense reclassification proposals.
    Atomic bulk-approve for HIGH; per-item for MED; LOW stays manual."""
    from . import suspense_reclassifier as _sr
    s = db.SessionLocal()
    try:
        buckets = _sr.scan(s)
    finally:
        s.close()

    def _row(p) -> str:
        susp_cls = "amt-out" if p.amount > 0 else "amt-in"
        proposed = f"{p.proposed_coa} <span class='conf'>({p.proposed_label})</span>" \
            if p.proposed_coa else f'<span class="conf">{p.reason}</span>'
        return f"""
        <tr>
          <td>{p.journal_date}</td>
          <td><code class="conf">j#{p.journal_id}</code></td>
          <td class="{susp_cls}">{p.amount:+,.2f}</td>
          <td>{(p.narration or "")[:90]}</td>
          <td>→ {proposed}</td>
        </tr>"""

    high = buckets.get("HIGH", [])
    med = buckets.get("MED", [])
    low = buckets.get("LOW", [])
    high_total = sum(abs(p.amount) for p in high)
    med_total = sum(abs(p.amount) for p in med)
    low_total = sum(abs(p.amount) for p in low)

    high_rows = "".join(_row(p) for p in high) or \
        '<tr><td colspan="5" style="text-align:center;color:#8e8e93;padding:14px;">No HIGH-confidence proposals.</td></tr>'
    med_rows = "".join(_row(p) for p in med) or \
        '<tr><td colspan="5" style="text-align:center;color:#8e8e93;padding:14px;">No MEDIUM-confidence proposals.</td></tr>'
    low_rows = "".join(_row(p) for p in low) or \
        '<tr><td colspan="5" style="text-align:center;color:#8e8e93;padding:14px;">No LOW-confidence items.</td></tr>'

    high_btn = f"""
        <form method="post" action="/reconcile/suspense/apply_high" style="margin-top:10px;">
          <button type="submit" style="font-size:14px; padding:8px 16px;">
            Approve all {len(high)} HIGH-confidence reclassifications (${high_total:,.2f})
          </button>
        </form>""" if high else ""

    body = f"""
    <div class="legend">
      <b>Suspense reclassifier (v2.26).</b> Journals where the contra leg
      landed in 1190 Suspense because the parser couldn't classify at
      post-time. Re-applying the canonical RULES catches them.
      <b>HIGH</b> = exact pattern match (1-click approve all).
      <b>MED</b> = partial match (review per-item).
      <b>LOW</b> = no match (manual triage required).
      Reclassification updates the GL line's account_id; journal identity
      preserved (external_id, journal_no, date unchanged).
    </div>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div><b style="color:#4cd964;">HIGH</b>
             <span class="conf">&nbsp; {len(high)} proposals · ${high_total:,.2f}</span></div>
      </div>
      <table>
        <thead><tr>
          <th>Date</th><th>Journal</th><th>Amount</th>
          <th>Narration</th><th>Proposed CoA</th>
        </tr></thead>
        <tbody>{high_rows}</tbody>
      </table>
      {high_btn}
    </div>

    <div class="card">
      <div><b style="color:#ffcc00;">MEDIUM</b>
           <span class="conf">&nbsp; {len(med)} proposals · ${med_total:,.2f}</span></div>
      <table>
        <thead><tr>
          <th>Date</th><th>Journal</th><th>Amount</th>
          <th>Narration</th><th>Proposed CoA</th>
        </tr></thead>
        <tbody>{med_rows}</tbody>
      </table>
    </div>

    <div class="card">
      <div><b style="color:#8e8e93;">LOW</b>
           <span class="conf">&nbsp; {len(low)} unclassifiable · ${low_total:,.2f}</span></div>
      <table>
        <thead><tr>
          <th>Date</th><th>Journal</th><th>Amount</th>
          <th>Narration</th><th>Status</th>
        </tr></thead>
        <tbody>{low_rows}</tbody>
      </table>
    </div>
    """
    title = "Suspense Reclassifier"
    meta = (f"{len(high)} HIGH · {len(med)} MED · {len(low)} LOW · "
            f"total ${high_total + med_total + low_total:,.2f}")
    return _layout(title, meta, body)


def apply_suspense_high() -> tuple[bool, str]:
    """Bulk-apply all HIGH-confidence Suspense reclassifications."""
    from . import suspense_reclassifier as _sr
    s = db.SessionLocal()
    try:
        buckets = _sr.scan(s)
        high = buckets.get("HIGH", [])
        if not high:
            return False, "no HIGH-confidence proposals to apply"
        summary = _sr.apply_proposals(s, high)
        return summary["applied"] > 0 and summary["errors"] == 0, (
            f"applied={summary['applied']}, errors={summary['errors']}"
        )
    finally:
        s.close()


# ──────────────────────────────────────────────────────────────────────────────
# /alerts (audit-7 Q3)
# ──────────────────────────────────────────────────────────────────────────────


def _render_alert_row(row) -> str:
    sev_cls = {"high": "pill-rejected", "medium": "pill-pending",
               "low": "pill-lapsed"}.get(row.severity, "pill-lapsed")
    detected = row.detected_at.isoformat()[:16] if row.detected_at else ""
    aid = row.id
    return f"""
    <tr>
      <td><span class="pill {sev_cls}">{row.severity}</span></td>
      <td><code class="conf">{row.kind}</code></td>
      <td>{row.account_code or '—'}</td>
      <td>{row.message}</td>
      <td><span class="conf">{detected}</span></td>
      <td>
        <form method="post" action="/alerts/{aid}/resolve" style="display:inline;">
          <button type="submit" title="Mark resolved (issue fixed)">Resolve</button>
        </form>
        <form method="post" action="/alerts/{aid}/dismiss" style="display:inline;">
          <button type="submit" class="reject" title="Dismiss (ignore — won't re-alert)">Dismiss</button>
        </form>
      </td>
    </tr>
    """


def render_alerts_page() -> str:
    s = db.SessionLocal()
    try:
        rows = s.execute(
            select(ledger.Alert)
            .where(ledger.Alert.status == "pending")
            .order_by(ledger.Alert.severity.desc(), ledger.Alert.detected_at.desc())
            .limit(200)
        ).scalars().all()
        tallies = {sev: 0 for sev in ("high", "medium", "low")}
        for r in rows:
            tallies[r.severity] = tallies.get(r.severity, 0) + 1
    finally:
        s.close()

    body_rows = "".join(_render_alert_row(r) for r in rows) or \
        '<tr><td colspan="6" style="text-align:center;color:#8e8e93;padding:20px;">No pending alerts.</td></tr>'
    body = f"""
    <div class="legend">
      Behavioural alerts from the daily scan. Resolve to close (re-detects
      if the underlying issue persists). Dismiss to ignore permanently.
    </div>
    <div class="card" style="display:flex;gap:24px;">
      <div><b style="color:#ff6b6b;">{tallies.get('high',0)}</b> <span class="conf">high</span></div>
      <div><b style="color:#ffcc00;">{tallies.get('medium',0)}</b> <span class="conf">medium</span></div>
      <div><b style="color:#8e8e93;">{tallies.get('low',0)}</b> <span class="conf">low</span></div>
    </div>
    <div class="card">
      <table>
        <thead><tr>
          <th>Sev</th><th>Kind</th><th>Account</th><th>Message</th>
          <th>Detected</th><th>Action</th>
        </tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>
    """
    return _layout("Alerts", f"{len(rows)} pending · scanned daily", body)


def alert_resolve(alert_id: int) -> tuple[bool, str]:
    s = db.SessionLocal()
    try:
        row = s.get(ledger.Alert, alert_id)
        if row is None:
            return False, f"alert {alert_id} not found"
        row.status = "resolved"
        row.resolved_at = db.now_utc()
        s.commit()
        return True, "resolved"
    except Exception as e:
        s.rollback()
        return False, f"error: {e!s}"
    finally:
        s.close()


def alert_dismiss(alert_id: int) -> tuple[bool, str]:
    s = db.SessionLocal()
    try:
        row = s.get(ledger.Alert, alert_id)
        if row is None:
            return False, f"alert {alert_id} not found"
        row.status = "dismissed"
        row.dismissed_at = db.now_utc()
        s.commit()
        return True, "dismissed"
    except Exception as e:
        s.rollback()
        return False, f"error: {e!s}"
    finally:
        s.close()


# ──────────────────────────────────────────────────────────────────────────────
# /facilities
# ──────────────────────────────────────────────────────────────────────────────


def render_facilities_page() -> str:
    s = db.SessionLocal()
    try:
        rows = s.execute(
            select(db.CreditFacility).order_by(
                db.CreditFacility.status,
                db.CreditFacility.lender_name,
            )
        ).scalars().all()
    finally:
        s.close()

    by_status: dict[str, list] = {}
    for r in rows:
        by_status.setdefault(r.status or "unknown", []).append(r)

    def _fmt(amt) -> str:
        if amt is None: return "—"
        return f"{float(amt):,.2f}"

    sections = []
    for status in ["active", "in_default", "restructured", "paid_off", "cancelled"]:
        items = by_status.get(status, [])
        if not items: continue
        total_out = sum(float(r.current_outstanding or 0) for r in items)
        total_lim = sum(float(r.credit_limit or 0) for r in items)
        body_rows = "".join(
            f"""<tr>
              <td>{r.lender_name}<br><span class="conf">{r.facility_type}</span></td>
              <td><code class="conf">{r.account_number or '—'}</code></td>
              <td class="amt">{_fmt(r.credit_limit)}</td>
              <td class="amt">{_fmt(r.current_outstanding)}</td>
              <td class="amt">{_fmt(r.eir_pct)}</td>
              <td><span class="conf">{r.maturity_date.date().isoformat() if r.maturity_date else '—'}</span></td>
            </tr>"""
            for r in items
        )
        pill_cls = {
            "active": "pill-active", "in_default": "pill-rejected",
            "paid_off": "pill-resolved", "cancelled": "pill-lapsed",
            "restructured": "pill-pending",
        }.get(status, "pill-lapsed")
        sections.append(f"""
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div><span class="pill {pill_cls}">{status}</span>
                 <span class="conf">&nbsp; {len(items)} facilities</span></div>
            <div class="conf">Limit <b style="color:#f0f0f0;">{total_lim:,.2f}</b>
                             &nbsp; Outstanding <b style="color:#ff6b6b;">{total_out:,.2f}</b></div>
          </div>
          <table>
            <thead><tr>
              <th>Lender / Type</th><th>Acct #</th><th>Limit</th>
              <th>Outstanding</th><th>EIR%</th><th>Maturity</th>
            </tr></thead>
            <tbody>{body_rows}</tbody>
          </table>
        </div>
        """)

    body = "\n".join(sections) or '<div class="card">No credit facilities recorded.</div>'

    return _layout("Credit Facilities",
                   f"{len(rows)} total · canonical owner: <code>credit_facilities</code> table",
                   body + f'<div class="legend" style="margin-top:14px;">'
                          f'See also: <a class="back" href="/admin/credit_utilization">utilization view</a></div>')


# ──────────────────────────────────────────────────────────────────────────────
# /policies
# ──────────────────────────────────────────────────────────────────────────────


def render_policies_page() -> str:
    s = db.SessionLocal()
    try:
        policies = s.execute(
            select(ledger.InsurancePolicyRegistry).order_by(
                ledger.InsurancePolicyRegistry.status,
                ledger.InsurancePolicyRegistry.insurer,
                ledger.InsurancePolicyRegistry.policy_ref,
            )
        ).scalars().all()

        # Latest NAV snapshot per policy_ref (single SQL roll-up)
        nav_latest_rows = s.execute(text("""
          SELECT a.policy_ref, a.snapshot_date, a.total_value, a.units_held, a.nav_per_unit
          FROM ilp_portfolio_snapshot a
          INNER JOIN (
            SELECT policy_ref, MAX(snapshot_date) AS d
            FROM ilp_portfolio_snapshot GROUP BY policy_ref
          ) b ON b.policy_ref = a.policy_ref AND b.d = a.snapshot_date
        """)).fetchall()
        nav_latest = {r[0]: r for r in nav_latest_rows}
    finally:
        s.close()

    def _fmt(v): return "—" if v is None else f"{float(v):,.2f}"

    kind_sections: dict[str, list] = {}
    for p in policies:
        kind_sections.setdefault(p.kind, []).append(p)

    sections = []
    for kind in sorted(kind_sections):
        items = kind_sections[kind]
        body_rows = []
        for p in items:
            nav = nav_latest.get(p.policy_ref)
            nav_html = (f"{_fmt(nav[2])} <span class='conf'>@ {nav[1]}</span>"
                        if nav else '<span class="conf">—</span>')
            status_cls = {"active": "pill-active", "lapsed": "pill-lapsed",
                          "surrendered": "pill-rejected", "matured": "pill-resolved"}.get(
                              p.status, "pill-lapsed")
            body_rows.append(f"""<tr>
              <td><code class="conf">{p.policy_ref}</code><br>{p.product_name or ''}</td>
              <td>{p.insurer}</td>
              <td class="amt">{_fmt(p.premium_amount)}</td>
              <td><span class="conf">{p.premium_frequency or '—'}</span></td>
              <td class="amt">{nav_html}</td>
              <td><span class="pill {status_cls}">{p.status}</span></td>
            </tr>""")
        total_premium = sum(float(p.premium_amount or 0) for p in items if p.status == "active")
        total_nav = sum(float(nav_latest[p.policy_ref][2] or 0)
                        for p in items if p.policy_ref in nav_latest)
        sections.append(f"""
        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div><b>{kind}</b> <span class="conf">&nbsp; {len(items)} policies</span></div>
            <div class="conf">Σ premium (active) <b style="color:#f0f0f0;">{total_premium:,.2f}</b>
                              &nbsp; Σ NAV <b style="color:#4cd964;">{total_nav:,.2f}</b></div>
          </div>
          <table>
            <thead><tr>
              <th>Policy</th><th>Insurer</th><th>Premium</th>
              <th>Freq</th><th>Latest NAV</th><th>Status</th>
            </tr></thead>
            <tbody>{''.join(body_rows)}</tbody>
          </table>
        </div>
        """)

    body = "\n".join(sections) or '<div class="card">No policies recorded.</div>'

    return _layout("Insurance & ILP Policies",
                   f"{len(policies)} policies · canonical owner: "
                   f"<code>insurance_policy_registry</code> + <code>ilp_portfolio_snapshot</code>",
                   body)
