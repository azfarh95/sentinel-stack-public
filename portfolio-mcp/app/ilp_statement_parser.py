"""ILP statement parser — Singlife Savvy Invest + Tokio Marine.

Live MV is auto-computed elsewhere (Morningstar SG NAV scraper + funds.yaml
units × NAV). This parser handles what the scraper CAN'T:

  1. Confirm unit counts in funds.yaml match the statement (variance alert)
  2. Capture per-period premium received + charges deducted
  3. Post the charges journal (admin + supplementary charges)
  4. Optionally update funds.yaml unit count if it has drifted

Journal model (per statement period):
  DR Insurance Expense (5310)           sum of admin + supplementary charges
  CR ILP asset (1222 Singlife / 1221 Tokio)   same amount (unit deduction)

Premiums received are NOT posted here — those come in via Firefly bridge as
POSB → ILP transfers (already captured in GL).

Mark-to-market revaluation is NOT posted here — funds.yaml + morningstar_sg.py
handle that on a daily cadence.

Run:
    docker exec portfolio-mcp python -m app.ilp_statement_parser <file.pdf>
    docker exec portfolio-mcp python -m app.ilp_statement_parser <file> --post
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path

from . import cc_statement_parser as ccp
from . import database as db
from . import journal_service as js

logger = logging.getLogger(__name__)


# Singlife policy → CoA (header — actual posting routes to per-fund leaves)
POLICY_COA = {
    "P4064051": ("1222", "Singlife Savvy Invest"),  # parent header; leaves 12221-12223 + 12229
    # Add Tokio policy number when first Tokio statement parses (parent 1221, leaves 12211-12215 + 12219)
}


@dataclass
class FundLine:
    fund_name: str
    opening_units: float
    closing_units: float
    opening_value: float
    closing_value: float
    premium_amount: float            # Reg.Premium contribution this period (cash IN)
    charges_amount: float            # Admin + Supplementary (cash OUT, unit deduction)
    other_amount: float              # Dividends, free units, etc.


@dataclass
class ParsedILPStatement:
    provider: str                    # "singlife" | "tokio"
    policy_number: str
    statement_date: _date | None
    period_start: _date | None
    period_end: _date | None
    plan_name: str
    coa_code: str                    # 1222 or 1221
    total_premiums_received_to_date: float
    death_benefit: float
    surrender_value: float
    funds: list[FundLine] = field(default_factory=list)
    source_path: str = ""
    parse_errors: list[str] = field(default_factory=list)

    @property
    def total_premium_this_period(self) -> float:
        return sum(f.premium_amount for f in self.funds)

    @property
    def total_charges_this_period(self) -> float:
        return sum(f.charges_amount for f in self.funds)

    @property
    def total_closing_value(self) -> float:
        return sum(f.closing_value for f in self.funds)

    def statement_id(self) -> str:
        d = self.period_end.isoformat() if self.period_end else "nodate"
        return f"ilp|{self.policy_number}|{d}"


def _money(s: str | None) -> float:
    if not s: return 0.0
    s = s.replace(",", "").replace("$", "").rstrip("-").strip()
    try: return float(s)
    except Exception: return 0.0


def _parse_singlife(text: str, pdf_path: str) -> ParsedILPStatement:
    """Singlife statement text is heavily concatenated (no spaces between words).
    Format clues: PolicyNumber, BasicDeathBenefit, TotalPremiumsReceived, per-fund
    sections with OpeningBalance / Reg.Premium / Charges / ClosingBalance rows."""
    out = ParsedILPStatement(
        provider="singlife", policy_number="", statement_date=None,
        period_start=None, period_end=None, plan_name="Singlife Savvy Invest",
        coa_code="12229",  # parent 1222 now header; Unallocated catch-all (per-fund route in post_ilp_journal)
        total_premiums_received_to_date=0, death_benefit=0, surrender_value=0,
        source_path=pdf_path,
    )

    # Policy number
    m = re.search(r"PolicyNumber\s*:?\s*([A-Z0-9]+)", text)
    if m: out.policy_number = m.group(1)

    # Period
    m = re.search(r"\((\d{1,2}\s*[A-Z][a-z]+\s*\d{4})\s*to\s*(\d{1,2}\s*[A-Z][a-z]+\s*\d{4})\)", text)
    if m:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                out.period_start = datetime.strptime(m.group(1).strip(), fmt).date()
                out.period_end = datetime.strptime(m.group(2).strip(), fmt).date()
                break
            except Exception:
                pass
    # Statement date (top of doc, e.g. "13April2026" or "13 April 2026")
    m = re.search(r"(\d{1,2})\s*([A-Z][a-z]+)\s*(\d{4})\b", text[:300])
    if m:
        try:
            out.statement_date = datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y").date()
        except Exception:
            pass

    # Summary numbers (handle concatenated form: "TotalPremiumsReceived-RegularPremium 8,500.00")
    m = re.search(r"TotalPremiums?Received[^\d]*?([\d,]+\.\d{2})", text)
    if m: out.total_premiums_received_to_date = _money(m.group(1))
    m = re.search(r"BasicDeathBenefit\s+([\d,]+\.\d{2})", text)
    if m: out.death_benefit = _money(m.group(1))
    m = re.search(r"TotalNetCashSurrenderValue\s+([\d,]+\.\d{2})", text)
    if m: out.surrender_value = _money(m.group(1))

    # Per-fund parsing. Singlife format per fund block:
    #   <FundName>
    #   OpeningBalance 12/03/2026 SGD <opening_units> <price> <opening_value>
    #   Reg.Premium 16/03/2026 SGD 1.0000 <units_alloc> <price> <amount>
    #   AdministrativeCharge 26/03/2026 SGD 1.0000 <units_neg>- <price> <amount>-
    #   SupplementaryCharge 26/03/2026 SGD 1.0000 <units_neg>- <price> <amount>-
    #   (optional Dividend/Bonus rows)
    #   ClosingBalance 12/04/2026 SGD <closing_units> <price> <closing_value>
    fund_block_re = re.compile(
        r"^([A-Za-z][A-Za-z0-9\s\.&,()-]{6,80}?)\s*"      # fund name on its own line
        r"OpeningBalance\s+\d{2}/\d{2}/\d{4}\s+SGD\s+([\d,]+\.\d+)\s+[\d.]+\s+([\d,]+\.\d{2})"
        r".*?"
        r"ClosingBalance\s+\d{2}/\d{2}/\d{4}\s+SGD\s+([\d,]+\.\d+)\s+[\d.]+\s+([\d,]+\.\d{2})",
        re.M | re.S,
    )
    for m in fund_block_re.finditer(text):
        fund_name = m.group(1).strip()
        block = m.group(0)
        opening_units = _money(m.group(2))
        opening_value = _money(m.group(3))
        closing_units = _money(m.group(4))
        closing_value = _money(m.group(5))
        # Premium contributions in block
        premium = sum(_money(mm.group(1)) for mm in
                      re.finditer(r"Reg\.Premium\s+\d{2}/\d{2}/\d{4}\s+SGD\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+([\d,]+\.\d{2})", block))
        # Charges (admin + supplementary) — amounts end with '-'
        charges = sum(_money(mm.group(1)) for mm in
                      re.finditer(r"(?:Administrative|Supplementary)Charge\s+\d{2}/\d{2}/\d{4}\s+SGD\s+[\d.]+\s+[\d.]+\-?\s+[\d.]+\s+([\d,]+\.\d{2})\-?", block))
        # Dividends/bonus units (positive contributions)
        other = sum(_money(mm.group(1)) for mm in
                    re.finditer(r"Dividend\(UnitsAlloc\.\)\s+\d{2}/\d{2}/\d{4}\s+SGD\s+([\d,]+\.\d{2})", block))
        out.funds.append(FundLine(
            fund_name=fund_name, opening_units=opening_units, closing_units=closing_units,
            opening_value=opening_value, closing_value=closing_value,
            premium_amount=premium, charges_amount=charges, other_amount=other,
        ))

    if not out.funds:
        out.parse_errors.append("no fund blocks matched — Singlife format may have changed")
    return out


def detect_and_parse(pdf_path: str) -> ParsedILPStatement | None:
    text = ccp._extract_text_smart(pdf_path)
    if "singlife" in text[:300].lower() and "savvy" in text.lower():
        return _parse_singlife(text, pdf_path)
    # Tokio support deferred until first PDF arrives
    return None


# ── Journal posting ────────────────────────────────────────────────────────


INSURANCE_EXPENSE = "5310"   # Insurance / ILP charges
INVESTMENT_INCOME = "4500"   # used for fund mtm if posted (not in this parser)
UNALLOCATED_BY_PROVIDER = {
    "tokio": "12219", "singlife": "12229",
}


def _build_fund_name_to_coa(policy_label: str) -> dict:
    """Read funds.yaml → return {normalized_fund_name: coa_code} for one policy."""
    import yaml
    try:
        cfg = yaml.safe_load(open("/finance/funds.yaml"))
    except Exception:
        return {}
    out = {}
    for f in cfg.get("funds", []):
        for h in f.get("holdings", []):
            if h.get("policy") == policy_label and h.get("coa_code"):
                # Normalize for fuzzy matching: lowercase, strip spaces + punctuation
                norm = "".join(c for c in f["name"].lower() if c.isalnum())
                out[norm] = h["coa_code"]
                # Also map by fund id (shorter) as fallback
                out[f["id"].lower().replace("_", "")] = h["coa_code"]
    return out


def _match_fund_to_coa(fund_stmt_name: str, name_map: dict, provider: str) -> str:
    """Best-effort match of statement fund name to a coa_code. Falls back to
    provider-specific Unallocated leaf."""
    norm = "".join(c for c in fund_stmt_name.lower() if c.isalnum())
    if norm in name_map:
        return name_map[norm]
    # Prefix match (statement names often abbreviated; e.g. "AllianzIncandGrowth")
    for k, v in name_map.items():
        if norm[:12] and norm[:12] in k:
            return v
        if k[:12] in norm:
            return v
    return UNALLOCATED_BY_PROVIDER.get(provider, "12229")


def post_ilp_journal(s, parsed: ParsedILPStatement) -> int | None:
    """Post per-fund charges journal. Each fund's admin + supplementary charges
    DR Insurance Expense, CR the fund's specific asset code (or Unallocated).
    Premium IN flows via Firefly bridge (POSB→ILP transfer).
    """
    if parsed.period_end is None or parsed.total_charges_this_period == 0:
        return None

    policy_label = parsed.plan_name  # e.g. "Singlife Savvy Invest"
    name_map = _build_fund_name_to_coa(policy_label)

    lines = []
    for fund in parsed.funds:
        if fund.charges_amount == 0:
            continue
        coa = _match_fund_to_coa(fund.fund_name, name_map, parsed.provider)
        lines.append({"account_code": INSURANCE_EXPENSE, "debit": fund.charges_amount,
                      "narration": f"ILP charge: {fund.fund_name[:50]}"})
        lines.append({"account_code": coa, "credit": fund.charges_amount,
                      "narration": f"Unit deduction: {fund.fund_name[:50]}"})

    if not lines:
        return None

    ext = f"ilp_charges:{parsed.policy_number}:{parsed.period_end.strftime('%Y-%m')}"
    return js.post_journal(
        s,
        journal_date=parsed.period_end,
        narration=f"ILP charges {parsed.plan_name} {parsed.period_end.strftime('%b %Y')}",
        journal_type="ilp_charges",
        lines=lines,
        source_doc="ILP_STMT",
        source_ref=parsed.statement_id(),
        external_id=ext,
    )


def check_unit_variance(parsed: ParsedILPStatement, funds_yaml_path: str = "/finance/funds.yaml") -> list[str]:
    """Compare statement closing_units vs funds.yaml holdings. Return alerts."""
    import yaml
    alerts = []
    try:
        cfg = yaml.safe_load(open(funds_yaml_path))
    except Exception as e:
        return [f"could not read funds.yaml: {e}"]
    policy_label_map = {"P4064051": "Singlife Savvy Invest"}
    policy_label = policy_label_map.get(parsed.policy_number)
    if not policy_label:
        return [f"no funds.yaml policy mapping for {parsed.policy_number}"]
    yaml_holdings = {}
    for f in cfg.get("funds", []):
        for h in f.get("holdings", []):
            if h.get("policy") == policy_label:
                yaml_holdings[f["name"].lower()] = h.get("units", 0)
    for stmt_fund in parsed.funds:
        # Fuzzy match against YAML fund names
        matched = False
        for yname, yunits in yaml_holdings.items():
            # Strip common abbreviations + spaces for matching
            stmt_norm = stmt_fund.fund_name.lower().replace(" ", "")
            yaml_norm = yname.replace(" ", "")
            if stmt_norm[:15] == yaml_norm[:15] or stmt_norm in yaml_norm or yaml_norm in stmt_norm:
                if abs(yunits - stmt_fund.closing_units) > 0.01:
                    alerts.append(
                        f"{stmt_fund.fund_name}: yaml={yunits:.5f} vs stmt={stmt_fund.closing_units:.5f} "
                        f"(Δ={stmt_fund.closing_units - yunits:+.5f})"
                    )
                matched = True
                break
        if not matched:
            alerts.append(f"{stmt_fund.fund_name}: not in funds.yaml")
    return alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="ILP statement PDF or folder")
    ap.add_argument("--post", action="store_true", help="Post charges journal")
    ap.add_argument("--check-units", action="store_true",
                    help="Check unit-count variance against funds.yaml")
    args = ap.parse_args()

    target = Path(args.target)
    files = []
    if target.is_dir():
        files = sorted(target.glob("*.pdf"))
    else:
        files = [target]

    db.init_db()
    sess = db.SessionLocal() if args.post else None
    print(f"{'File':<48} {'Policy':<12} {'Period':<24} {'Funds':>5} {'Premium':>9} {'Charges':>9} {'TotalMV':>10}")
    print("-" * 125)
    try:
        for f in files:
            p = detect_and_parse(str(f))
            if not p:
                continue
            period = f"{p.period_start} → {p.period_end}" if p.period_start else "?"
            jid = ""
            if args.post and sess:
                try:
                    jid = post_ilp_journal(sess, p) or "skipped"
                    sess.commit()
                except Exception as e:
                    jid = f"ERR:{str(e)[:30]}"
                    sess.rollback()
            print(f"  {f.name[:46]:<48} {p.policy_number:<12} {period:<24} "
                  f"{len(p.funds):>5} {p.total_premium_this_period:>9,.2f} "
                  f"{p.total_charges_this_period:>9,.2f} {p.total_closing_value:>10,.2f}  {jid}")
            for fd in p.funds:
                print(f"      • {fd.fund_name[:36]:<38} {fd.opening_units:>10.5f}u → "
                      f"{fd.closing_units:>10.5f}u  prem={fd.premium_amount:>6,.2f}  "
                      f"charge={fd.charges_amount:>5,.2f}")
            for e in p.parse_errors:
                print(f"      ⚠ {e}")
            if args.check_units:
                for a in check_unit_variance(p):
                    print(f"      Δ {a}")
    finally:
        if sess:
            sess.close()


if __name__ == "__main__":
    main()
