"""Suspense (1190) bulk reclassifier — v2.26.

Per Perplexity pass-11 Q1: when system-generated noise lands in Suspense
because the parser/classifier failed at post-time, the user shouldn't
manually triage each entry. The right pattern is:

  1. Walk every journal whose contra is 1190 Suspense
  2. Apply the canonical classifier rules (parser RULES +
     recurring_obligation_registry + own-account transfer patterns)
  3. Score each match HIGH / MED / LOW by confidence
  4. Surface to /reconcile/suspense with atomic bulk-approve for HIGH,
     per-item for MED, manual for LOW

The system fixes its own mistakes. User signs off in bulk.

Reclassification mechanic: UPDATE the GL line where account_id=1190
to the correct CoA. Preserves journal identity (external_id, journal_no,
date) — only the contra leg moves. Same approach as the audit-7 inv20
Savvy Invest fix.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from datetime import date
from typing import Iterable

from sqlalchemy import text

from . import database as db
from .posb_pdf_to_gl import RULES as POSB_RULES

logger = logging.getLogger(__name__)

SUSPENSE_COA = "1190"

# Additional patterns the POSB parser doesn't have for own-account transfers.
EXTRA_PATTERNS: list[tuple[str, str, str, str]] = [
    (r"MSL:\d+|MSL\s+\d", "1114", "transfer", "I-BANK transfer to Maybank"),
    (r"SCL:\d+|SCL\s+\d", "1115", "transfer", "I-BANK transfer to SC"),
]

ALL_RULES = list(POSB_RULES) + EXTRA_PATTERNS


@dataclass
class ReclassProposal:
    journal_id: int
    journal_date: str
    narration: str
    amount: float                 # signed: positive = was Dr 1190
    proposed_coa: str
    proposed_kind: str
    proposed_label: str
    confidence: str               # 'HIGH' | 'MED' | 'LOW'
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(narration: str | None) -> tuple[str, str | None, str | None, str]:
    """Return (confidence, coa, kind, reason) for a narration.

    HIGH: a parser RULES pattern matches directly.
    MED:  partial substring match on an all-caps token from a rule's pattern.
    LOW:  no match.
    """
    if not narration:
        return ("LOW", None, None, "empty narration")
    for pat, coa, kind, label in ALL_RULES:
        if re.search(pat, narration, re.IGNORECASE):
            return ("HIGH", coa, kind, label)
    # Partial substring fallback for MED — extract capitalised vendor tokens
    # from the regex and check if they appear in the narration.
    upper = narration.upper()
    for pat, coa, kind, label in ALL_RULES:
        words = re.findall(r"[A-Z]{4,}", pat)
        for w in words:
            if w in upper:
                return ("MED", coa, kind, f"partial: '{w}' in {label}")
    return ("LOW", None, None, "no pattern match")


def scan(s, since: date | None = None) -> dict[str, list[ReclassProposal]]:
    """Walk every posted journal whose contra is Suspense.
    Return {'HIGH': [...], 'MED': [...], 'LOW': [...]}.

    Each Suspense journal must have exactly 2 lines (Suspense + one main
    account); multi-line journals are not reclassifiable by this tool and
    appear in LOW with reason='multi-line journal'.
    """
    where_since = ""
    params: dict = {}
    if since is not None:
        where_since = "AND j.journal_date > :since"
        params["since"] = since.isoformat()

    rows = s.execute(text(f"""
      SELECT j.id, j.journal_date, j.narration,
             ROUND(SUM(gl_susp.debit_sgd - gl_susp.credit_sgd), 2) AS susp_net
      FROM journals j
      JOIN general_ledger gl_susp ON gl_susp.journal_id=j.id
          AND gl_susp.account_id=(SELECT id FROM chart_of_accounts WHERE account_code=:susp)
      WHERE j.status='posted' {where_since}
      GROUP BY j.id, j.journal_date, j.narration
      ORDER BY j.journal_date DESC, j.id DESC
    """), {**params, "susp": SUSPENSE_COA}).fetchall()

    buckets: dict[str, list[ReclassProposal]] = {"HIGH": [], "MED": [], "LOW": []}
    for jid, jdate, narr, susp_net in rows:
        # Refuse to touch journals with >2 lines (e.g. composite transfers)
        n_lines = s.execute(text(
            "SELECT COUNT(*) FROM general_ledger WHERE journal_id=:j"
        ), {"j": jid}).scalar() or 0
        if n_lines != 2:
            buckets["LOW"].append(ReclassProposal(
                journal_id=jid, journal_date=str(jdate), narration=narr or "",
                amount=float(susp_net), proposed_coa=None, proposed_kind=None,
                proposed_label=None,
                confidence="LOW", reason=f"multi-line journal ({n_lines} lines)",
            ))
            continue

        conf, coa, kind, reason = _classify(narr)
        prop = ReclassProposal(
            journal_id=jid, journal_date=str(jdate), narration=narr or "",
            amount=float(susp_net),
            proposed_coa=coa, proposed_kind=kind, proposed_label=reason,
            confidence=conf, reason=reason,
        )
        buckets[conf].append(prop)
    return buckets


def apply_proposals(s, proposals: Iterable[ReclassProposal]) -> dict:
    """Apply a batch of reclassifications. Atomic: all or nothing.

    For each proposal:
      - Find the GL line where journal_id matches AND account_id=1190
      - UPDATE its account_id to the proposed CoA's id
      - Annotate the journal narration with [RECLASSED <date>: 1190→<coa>]
    """
    summary = {"applied": 0, "errors": 0, "details": []}
    today = date.today().isoformat()

    coa_id_cache: dict[str, int] = {}
    def _coa_id(code: str) -> int | None:
        if code in coa_id_cache: return coa_id_cache[code]
        row = s.execute(text(
            "SELECT id FROM chart_of_accounts WHERE account_code=:c"
        ), {"c": code}).fetchone()
        coa_id_cache[code] = row[0] if row else None
        return coa_id_cache[code]

    for p in proposals:
        if not p.proposed_coa:
            summary["errors"] += 1
            summary["details"].append(f"j#{p.journal_id}: no proposed CoA")
            continue
        target_id = _coa_id(p.proposed_coa)
        if not target_id:
            summary["errors"] += 1
            summary["details"].append(
                f"j#{p.journal_id}: proposed CoA {p.proposed_coa} not in chart_of_accounts"
            )
            continue
        try:
            res = s.execute(text("""
              UPDATE general_ledger
              SET account_id=:tid
              WHERE journal_id=:j AND account_id=(
                SELECT id FROM chart_of_accounts WHERE account_code=:susp
              )
            """), {"tid": target_id, "j": p.journal_id, "susp": SUSPENSE_COA})
            if res.rowcount == 0:
                summary["errors"] += 1
                summary["details"].append(
                    f"j#{p.journal_id}: no Suspense GL line found to update"
                )
                continue
            s.execute(text("""
              UPDATE journals
              SET narration = '[RECLASSED ' || :today || ': 1190→' || :coa || '] ' || narration
              WHERE id=:j
            """), {"today": today, "coa": p.proposed_coa, "j": p.journal_id})
            summary["applied"] += 1
            summary["details"].append(
                f"j#{p.journal_id}: 1190 → {p.proposed_coa} ({p.proposed_label})"
            )
        except Exception as e:
            summary["errors"] += 1
            summary["details"].append(f"j#{p.journal_id}: {type(e).__name__}: {e!s}")

    if summary["errors"] == 0:
        s.commit()
    else:
        s.rollback()
        summary["committed"] = False
        return summary
    summary["committed"] = True
    return summary
