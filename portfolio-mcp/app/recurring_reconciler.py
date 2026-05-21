"""Recurring obligation reconciler — match POSB outflows in 1190/4900 suspense
against the recurring_obligation_registry.

Three pass strategy:
  1. SEED: read finance/recurring_obligations.yaml → upsert into the registry
  2. RECONCILE: for each 1190/4900 journal leg, see if amount+narration matches
     a registry row. On hit → void the suspense journal, re-post with the
     correct contra_coa.
  3. DETECT_ORPHANS: find ≥2 occurrences of (same amount, similar narration)
     in 1190 suspense with no registry hit. Flag in recurring_reconcile_log
     as "unknown recurring — what is it?" for user to register.

CLI:
    python -m app.recurring_reconciler --seed         # read YAML → DB
    python -m app.recurring_reconciler --scan         # report-only
    python -m app.recurring_reconciler --apply        # void+repost
    python -m app.recurring_reconciler --orphans      # detect recurring unknowns
    python -m app.recurring_reconciler --status       # show registry + log
"""
from __future__ import annotations
import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import text

from app import database as db


REGISTRY_YAML_PATHS = [
    Path("/finance/recurring_obligations.yaml"),
    Path("/app/finance/recurring_obligations.yaml"),
    Path(__file__).parent.parent / "finance" / "recurring_obligations.yaml",
]


def _ensure_tables(s):
    """Create both registry + log tables if missing."""
    s.execute(text("""
      CREATE TABLE IF NOT EXISTS recurring_obligation_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name VARCHAR NOT NULL UNIQUE,
        kind VARCHAR NOT NULL,
        contra_coa VARCHAR NOT NULL,
        direction VARCHAR DEFAULT 'out',
        expected_amount FLOAT NOT NULL,
        amount_tolerance FLOAT DEFAULT 0.50,
        frequency VARCHAR DEFAULT 'monthly',
        expected_day_of_month INTEGER,
        grace_days INTEGER DEFAULT 5,
        identifier_patterns VARCHAR,
        counterparty_hint VARCHAR,
        journal_kind VARCHAR DEFAULT 'expense',
        active_from DATE,
        active_to DATE,
        notes VARCHAR,
        last_seen_journal_id INTEGER,
        last_seen_amount FLOAT,
        last_seen_date DATE,
        drift_alerts INTEGER DEFAULT 0,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
      )
    """))
    s.execute(text("CREATE INDEX IF NOT EXISTS ix_recurob_kind_active ON recurring_obligation_registry(kind, active_from, active_to)"))
    s.execute(text("""
      CREATE TABLE IF NOT EXISTS recurring_reconcile_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status VARCHAR NOT NULL,
        obligation_id INTEGER,
        journal_id INTEGER,
        voided_journal_id INTEGER,
        tx_date DATE,
        tx_amount FLOAT,
        expected_amount FLOAT,
        counterparty VARCHAR,
        notes VARCHAR,
        created_at DATETIME NOT NULL
      )
    """))
    s.execute(text("CREATE INDEX IF NOT EXISTS ix_recurlog_status ON recurring_reconcile_log(status)"))
    s.commit()


def _registry_yaml_path() -> Path:
    for p in REGISTRY_YAML_PATHS:
        if p.exists():
            return p
    raise FileNotFoundError(f"recurring_obligations.yaml not found in {REGISTRY_YAML_PATHS}")


def seed_from_yaml(s) -> int:
    """Upsert every obligation from YAML. Returns count synced."""
    path = _registry_yaml_path()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    obs = data.get("obligations", []) or []
    n = 0
    for o in obs:
        params = {
            "name": o["name"], "kind": o["kind"],
            "contra_coa": o["contra_coa"], "direction": o.get("direction", "out"),
            "expected_amount": float(o["expected_amount"] or 0.0),
            "amount_tolerance": float(o.get("amount_tolerance", 0.50)),
            "frequency": o.get("frequency", "monthly"),
            "expected_day_of_month": o.get("expected_day_of_month"),
            "grace_days": int(o.get("grace_days", 5)),
            "identifier_patterns": json.dumps(o.get("identifier_patterns", [])),
            "counterparty_hint": o.get("counterparty_hint", ""),
            "journal_kind": o.get("journal_kind", "expense"),
            "notes": o.get("notes", ""),
        }
        existing = s.execute(
            text("SELECT id FROM recurring_obligation_registry WHERE name=:name"),
            {"name": params["name"]}
        ).fetchone()
        if existing:
            s.execute(text("""
              UPDATE recurring_obligation_registry
              SET kind=:kind, contra_coa=:contra_coa, direction=:direction,
                  expected_amount=:expected_amount, amount_tolerance=:amount_tolerance,
                  frequency=:frequency, expected_day_of_month=:expected_day_of_month,
                  grace_days=:grace_days, identifier_patterns=:identifier_patterns,
                  counterparty_hint=:counterparty_hint, journal_kind=:journal_kind,
                  notes=:notes, updated_at=CURRENT_TIMESTAMP
              WHERE name=:name
            """), params)
        else:
            s.execute(text("""
              INSERT INTO recurring_obligation_registry
                (name, kind, contra_coa, direction, expected_amount, amount_tolerance,
                 frequency, expected_day_of_month, grace_days, identifier_patterns,
                 counterparty_hint, journal_kind, notes, created_at, updated_at)
              VALUES (:name, :kind, :contra_coa, :direction, :expected_amount,
                      :amount_tolerance, :frequency, :expected_day_of_month,
                      :grace_days, :identifier_patterns, :counterparty_hint,
                      :journal_kind, :notes, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """), params)
        n += 1
    s.commit()
    return n


def _load_obligations(s) -> list[dict]:
    rows = s.execute(text("""
      SELECT id, name, kind, contra_coa, expected_amount, amount_tolerance,
             identifier_patterns, journal_kind, counterparty_hint
      FROM recurring_obligation_registry
    """)).all()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "name": r[1], "kind": r[2], "contra_coa": r[3],
            "expected_amount": float(r[4] or 0), "amount_tolerance": float(r[5] or 0.5),
            "patterns": json.loads(r[6] or "[]"), "journal_kind": r[7],
            "counterparty_hint": r[8] or "",
        })
    return out


def _suspense_outflows(s) -> list[dict]:
    """POSB outflows currently sitting in 1190/4900 (lifestyle-lump already
    siphoned the obvious POS/debit-card stuff to 5190)."""
    rows = s.execute(text("""
      SELECT j.id, j.journal_date, j.narration, gl.debit, coa.account_code
      FROM journals j
      JOIN general_ledger gl ON gl.journal_id = j.id
      JOIN chart_of_accounts coa ON coa.id = gl.account_id
      WHERE j.status = 'posted'
        AND j.source_doc = 'POSB_PDF_DIRECT'
        AND coa.account_code IN ('1190', '4900')
        AND gl.debit > 0
      ORDER BY j.journal_date
    """)).all()
    return [
        {"jid": r[0], "date": r[1], "narration": r[2] or "",
         "amount": float(r[3] or 0), "current_coa": r[4]}
        for r in rows
    ]


RECURRING_TX_TYPE_HINTS = ("GIRO", "STANDING INSTRUCTION", "FAST PAYMENT",
                            "FAST COLLECTION", "FAST RECEIPT", "PAYMENTS / COLLECTIONS")


def _matches(tx_narration: str, tx_amount: float, obligation: dict) -> bool:
    """Return True if this tx matches this obligation.

    Match strategy:
      • Obligations with placeholder expected_amount <= 0 are inactive (skipped).
      • Amount within tolerance is required.
      • If any identifier_pattern hits the narration → match (high confidence).
      • Else fall back: if narration contains a recurring-tx-type hint
        (GIRO, Standing Instruction, FAST, etc.), accept on amount alone.
        This covers cases where the bank statement strips the merchant name
        (typical for POSB GIRO) so the carrier-based identifier never gets
        captured but the user has confirmed amount × frequency is unique.
    """
    # Skip placeholder obligations
    if obligation["expected_amount"] <= 0.0:
        return False
    if abs(tx_amount - obligation["expected_amount"]) > obligation["amount_tolerance"]:
        return False
    narr_up = (tx_narration or "").upper()
    # Pattern hit?
    for pat in obligation["patterns"]:
        if not pat: continue
        try:
            if re.search(pat, narr_up, re.IGNORECASE):
                return True
        except re.error:
            if pat.upper() in narr_up:
                return True
    # Amount-only fallback: only if the narration carries a recurring-style hint
    for hint in RECURRING_TX_TYPE_HINTS:
        if hint in narr_up:
            return True
    return False


def scan(s, dry: bool = True) -> dict:
    obligations = _load_obligations(s)
    suspense = _suspense_outflows(s)
    matched = []
    for tx in suspense:
        for ob in obligations:
            if _matches(tx["narration"], tx["amount"], ob):
                matched.append((tx, ob))
                break
    return {
        "obligations_n": len(obligations),
        "suspense_n": len(suspense),
        "matched_n": len(matched),
        "matches": matched,
    }


def apply_matches(s, matches: list[tuple]) -> int:
    """For each (tx, obligation) — void the original journal and post a
    replacement with the right contra_coa. Records in recurring_reconcile_log."""
    from app import journal_service as js
    n = 0
    for tx, ob in matches:
        # Get the original journal's legs so we can rebuild correctly
        legs = s.execute(text("""
          SELECT coa.account_code, gl.debit, gl.credit, gl.narration
          FROM general_ledger gl
          JOIN chart_of_accounts coa ON coa.id = gl.account_id
          WHERE gl.journal_id = :j
        """), {"j": tx["jid"]}).all()
        # Identify POSB leg (1111) and suspense leg
        posb_leg = next((l for l in legs if l[0] == "1111"), None)
        if not posb_leg:
            continue
        # POSB was credited (outflow), suspense (1190/4900) debited
        amt = float(posb_leg[2] or 0)
        if amt < 0.01:
            continue
        # Void original
        s.execute(text("""
          UPDATE journals SET status='voided',
              voided_at=CURRENT_TIMESTAMP,
              voided_reason=:r
          WHERE id=:jid
        """), {"jid": tx["jid"], "r": f"recurring_reconciler: matched obligation '{ob['name']}'"})
        # Post replacement: Dr contra_coa / Cr POSB
        try:
            new_jid = js.post_journal(
                s,
                journal_date=datetime.fromisoformat(str(tx["date"])[:10]).date(),
                narration=f"[recurring] {ob['name']} (was 1190)",
                journal_type=ob["journal_kind"],
                lines=[
                    {"account_code": ob["contra_coa"], "debit": amt,
                     "narration": f"{ob['counterparty_hint']} — recurring match (jid was {tx['jid']})"},
                    {"account_code": "1111", "credit": amt,
                     "narration": "POSB"},
                ],
                source_doc="RECURRING_RECON",
                source_ref=f"obligation:{ob['id']}",
                external_id=f"recurring_repost:{tx['jid']}",
            )
            s.execute(text("""
              INSERT INTO recurring_reconcile_log
                (status, obligation_id, journal_id, voided_journal_id,
                 tx_date, tx_amount, expected_amount, counterparty, notes, created_at)
              VALUES ('matched', :ob, :nj, :vj, :td, :ta, :ea, :cp, :nt, CURRENT_TIMESTAMP)
            """), {
                "ob": ob["id"], "nj": new_jid, "vj": tx["jid"],
                "td": tx["date"], "ta": amt, "ea": ob["expected_amount"],
                "cp": ob["counterparty_hint"],
                "nt": f"repointed {tx['current_coa']}→{ob['contra_coa']}",
            })
            # Update obligation's last_seen
            s.execute(text("""
              UPDATE recurring_obligation_registry
              SET last_seen_journal_id=:j, last_seen_amount=:a, last_seen_date=:d,
                  updated_at=CURRENT_TIMESTAMP
              WHERE id=:ob
            """), {"j": new_jid, "a": amt, "d": tx["date"], "ob": ob["id"]})
            n += 1
        except Exception as e:
            print(f"  ERR rebuilding jid={tx['jid']}: {str(e)[:120]}")
            s.rollback()
    s.commit()
    return n


def detect_orphans(s, min_occurrences: int = 2) -> list[dict]:
    """Find recurring patterns in 1190/4900 suspense that DON'T match any registry.
    Groups by (rounded_amount, payee_substring)."""
    suspense = _suspense_outflows(s)
    obligations = _load_obligations(s)
    groups = defaultdict(list)
    for tx in suspense:
        # Skip ones that DO match an obligation (no need to flag them)
        if any(_matches(tx["narration"], tx["amount"], ob) for ob in obligations):
            continue
        # Group key: amount to cent + first 20 char narration prefix (after [direct POSB])
        narr = (tx["narration"] or "").replace("[direct POSB]", "").strip()
        narr_key = narr[:30].upper()
        groups[(round(tx["amount"], 2), narr_key)].append(tx)
    out = []
    for (amt, narr), txs in groups.items():
        if len(txs) >= min_occurrences:
            out.append({
                "amount": amt, "narration_key": narr,
                "occurrences": len(txs),
                "first_date": min(t["date"] for t in txs),
                "last_date": max(t["date"] for t in txs),
                "sample_jids": [t["jid"] for t in txs[:3]],
            })
    out.sort(key=lambda x: (-x["occurrences"], -x["amount"]))
    return out


def show_status(s):
    obs = s.execute(text("""
      SELECT kind, COUNT(*), SUM(last_seen_amount IS NOT NULL)
      FROM recurring_obligation_registry GROUP BY kind ORDER BY kind
    """)).all()
    print("\n=== Registry by kind ===")
    for r in obs:
        seen = int(r[2] or 0)
        print(f"  {r[0]:<14}  {r[1]:>2} obligations  ({seen} matched at least once)")
    log = s.execute(text("""
      SELECT status, COUNT(*), SUM(tx_amount) FROM recurring_reconcile_log
      GROUP BY status
    """)).all()
    print("\n=== Reconcile log ===")
    if not log:
        print("  (empty)")
    for r in log:
        print(f"  {r[0]:<14}  {r[1]:>3} rows  total=${float(r[2] or 0):,.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--orphans", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    db.init_db()
    s = db.SessionLocal()
    _ensure_tables(s)
    try:
        if args.seed:
            n = seed_from_yaml(s)
            print(f"Seeded {n} obligations from YAML")
        if args.scan:
            r = scan(s)
            print(f"\nObligations: {r['obligations_n']}  Suspense outflows: {r['suspense_n']}")
            print(f"Matched: {r['matched_n']}\n")
            for tx, ob in r["matches"][:25]:
                print(f"  jid={tx['jid']:<5} {tx['date']}  ${tx['amount']:>9,.2f}  →  {ob['name']:<35}  ({ob['contra_coa']})")
            if len(r["matches"]) > 25:
                print(f"  ... and {len(r['matches']) - 25} more")
        if args.apply:
            r = scan(s)
            n = apply_matches(s, r["matches"])
            print(f"\n[apply] repointed {n} journals to their registry obligation")
        if args.orphans:
            orphs = detect_orphans(s)
            print(f"\n⚠ {len(orphs)} unknown recurring patterns (≥2 occurrences, no registry hit):")
            for o in orphs[:20]:
                print(f"  ${o['amount']:>9,.2f} × {o['occurrences']:>3}  {o['first_date']}..{o['last_date']}  "
                      f"'{o['narration_key'][:50]}'")
            if not orphs:
                print("  (no unknown recurring patterns — coverage looks healthy)")
        if args.status:
            show_status(s)
        if not any([args.seed, args.scan, args.apply, args.orphans, args.status]):
            print("=== Run with --seed, --scan, --apply, --orphans, or --status ===")
            ap.print_help()
    finally:
        s.close()


if __name__ == "__main__":
    main()
