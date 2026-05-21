"""Single resolver for `tx → CoA` routing across every parser.

CONSUMES: `finance/account_directory.yaml`
USED BY:  posb_cutover_2026, maybank_cutover, sc_cutover, cc_*_parser, ilp_parser,
          payslip_parser, etc.

Replaces hardcoded `if/elif` chains in classify_tx() functions with a single
table-driven lookup whose source of truth is the YAML directory. When you add
a new bank or rename a CoA, edit the YAML; never touch parser code.

Returns:
    (coa: str, kind: str, reason: str, confidence: int)

Confidence levels:
    100 — exact identifier match (policy number, card number, account number, routing)
     90 — entity name or recipient pattern match
     75 — instalment amount-pinpoint match
     60 — tx_type marker fallback
     40 — generic category (insurance / F&B / etc.)
      0 — suspense (no rule matched)

Resolution order (highest confidence wins):
    1.  Insurance/ILP policy number
    2.  Bank account number / card number / routing
    3.  Counterparty / recipient name
    4.  Amount-pinpoint (only if tx_type matches expected)
    5.  Vendor/category patterns
    6.  Suspense (1190)

A `tx` is a dict with keys:
    amount: float
    tx_type: str
    direction: "in" | "out" | "unknown"
    carriers: dict — output of universal_pdf_parser
    raw_lines: list[str] — preserved continuation lines
    date_iso: str
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# Try mounted /finance, fall back to in-image path
DIRECTORY_PATHS = [
    Path("/finance/account_directory.yaml"),
    Path("/app/finance/account_directory.yaml"),
    Path(__file__).parent.parent / "finance" / "account_directory.yaml",
]


@dataclass
class RoutingResult:
    coa: str
    kind: str
    reason: str
    confidence: int

    def as_tuple(self):
        return (self.coa, self.kind, self.reason, self.confidence)


class AccountRouter:
    def __init__(self, directory_yaml: Optional[Path] = None):
        path = directory_yaml or self._find_directory()
        with open(path, "r", encoding="utf-8") as f:
            self.directory = yaml.safe_load(f)
        self.suspense_coa = self.directory.get("suspense", {}).get("coa", "1190")
        self._build_indices()

    @staticmethod
    def _find_directory() -> Path:
        for p in DIRECTORY_PATHS:
            if p.exists():
                return p
        raise FileNotFoundError(f"account_directory.yaml not found in {DIRECTORY_PATHS}")

    def _build_indices(self):
        """Pre-compute lookup tables from the directory for fast resolution."""
        self.by_policy = {}              # P4064051 → (coa, name)
        self.by_card = {}                # 4119110104972424 → (coa, name)
        self.by_account = {}             # 14030791138 → (coa, name)
        self.by_routing = {}             # MSL:14030791138:I-BANK → (coa, name)
        self.by_masked_last4 = {}        # "7004" → (coa, name)
        self.by_recipient = []           # [(pattern_upper, coa, name)]
        self.by_tx_type = {}             # "MEPS Receipt" → (coa, name, kind)
        self.by_instalment = []           # [(amount, coa, name, recipient_required)]
        self.vendor_categories = []       # [(coa, [patterns_upper])]

        def walk(node):
            """Recursively flatten the directory tree into the lookup indices."""
            if isinstance(node, dict):
                if "coa" in node and "identifiers" in node:
                    self._index_entry(node)
                if "coa" in node and "patterns" in node:
                    self._index_vendor_category(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.directory.get("assets", {}))
        walk(self.directory.get("liabilities", {}))
        walk(self.directory.get("income", []))
        walk(self.directory.get("expense", {}))

    def _index_entry(self, entry: dict):
        coa = entry["coa"]
        name = entry.get("name", "")
        ids = entry.get("identifiers", {})

        if pol := ids.get("policy_number"):
            self.by_policy[pol] = (coa, name)
        if pol := ids.get("policy_long_ref"):
            self.by_policy[pol] = (coa, name)

        for key in ("card_number", "bare_card"):
            if v := ids.get(key):
                normalised = re.sub(r"[-\s]", "", v)
                self.by_card[normalised] = (coa, name)

        for key in ("account_number", "bare_account_number", "bare_account",
                    "cashline_bare", "cashline_padded"):
            if v := ids.get(key):
                normalised = re.sub(r"[-\s]", "", str(v))
                self.by_account[normalised] = (coa, name)

        for key in ("msl_routing", "scl_routing", "gxs_routing", "dbsc_routing",
                    "ccc_routing_prefix"):
            if v := ids.get(key):
                self.by_routing[v.upper()] = (coa, name)
                # also index the prefix variant (first 4 chars of card)
                if key == "ccc_routing_prefix":
                    self.by_routing[f"CCC_PREFIX_{v}"] = (coa, name)

        if v := ids.get("masked_account_last4"):
            self.by_masked_last4[str(v)] = (coa, name)

        for key in ("recipient_pattern", "recipient_pattern_2"):
            if v := ids.get(key):
                self.by_recipient.append((v.upper(), coa, name))

        for key in ("tx_type_marker", "tx_type_marker_drawdown",
                    "tx_type_marker_2"):
            if v := ids.get(key):
                kind = "loan_in" if "drawdown" in key else "income"
                self.by_tx_type[v.upper()] = (coa, name, kind)

        if amt := ids.get("instalment_amount"):
            recipient_required = ids.get("recipient_pattern") or ""
            self.by_instalment.append((float(amt), coa, name, recipient_required.upper()))

        # tx_marker — generic uppercase string in tx_type or narration
        if v := ids.get("tx_marker"):
            self.by_tx_type[v.upper()] = (coa, name, "income")

    def _index_vendor_category(self, entry: dict):
        coa = entry["coa"]
        patterns = [p.upper() for p in (entry.get("patterns") or [])]
        if patterns:
            self.vendor_categories.append((coa, patterns))

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────
    def route(self, tx) -> RoutingResult:
        """Resolve a parsed transaction to a CoA leaf. Highest confidence wins."""
        carriers = tx.carriers if hasattr(tx, "carriers") else (tx.get("carriers") or {})
        tx_type = (tx.tx_type if hasattr(tx, "tx_type") else tx.get("tx_type", "")).upper()
        amount = tx.amount if hasattr(tx, "amount") else tx.get("amount", 0.0)
        direction = tx.direction if hasattr(tx, "direction") else tx.get("direction", "out")
        deposit_amt = tx.deposit_amount if hasattr(tx, "deposit_amount") else tx.get("deposit_amount", 0.0)

        # ── 1. Policy number (most specific) ──
        for k in ("insurance_policy_long_ref", "insurance_policy_ref"):
            if pol := carriers.get(k):
                if hit := self.by_policy.get(pol):
                    return RoutingResult(hit[0], "ilp_premium", hit[1], 100)

        # ── 2. Card number routing — DBSC / CCC / TO CARD / bare ──
        for k in ("dbs_card_routing", "ccc_card_routing", "to_card_routing"):
            if v := carriers.get(k):
                norm = re.sub(r"[-\s]", "", v)
                if hit := self.by_card.get(norm):
                    side = "cc_pay" if direction == "out" else "cc_charge"
                    return RoutingResult(hit[0], side, f"{hit[1]} via {k}", 100)

        # ── 2b. Masked-account last-4 (Maybank statements) ──
        last4 = carriers.get("masked_account_last4") or carriers.get("counterparty_with_last4")
        if last4 and len(str(last4)) >= 4:
            # If counterparty_with_last4 fired, value is the captured last-4 digits
            l4 = str(last4)[-4:]
            if hit := self.by_masked_last4.get(l4):
                kind = "cc_pay" if str(hit[0]).startswith("21") else \
                       "loan_pay" if str(hit[0]).startswith("22") else "transfer"
                return RoutingResult(hit[0], kind, f"{hit[1]} (masked last4)", 95)

        # ── 3. Account-number routing — MSL / SCL / GXS / cashline ──
        if v := carriers.get("maybank_routing_msl"):
            if hit := self.by_account.get(v):
                return RoutingResult(hit[0], "transfer", f"{hit[1]} (MSL)", 100)
        if v := carriers.get("sc_routing"):
            full = f"SCL:{v}:I-BANK"
            if hit := self.by_routing.get(full):
                return RoutingResult(hit[0], "loan_in", f"{hit[1]} (SCL)", 100)
        # IBFT routing — inbound DBS→SC via Balance Transfer = SC BT facility (CoA 2211)
        if v := (carriers.get("ibft_routing") or carriers.get("dbs_swift_routing")):
            if "DBSSSGSGBRT" in str(v) or "SCBLSG" in str(v) or "BRT" in str(v):
                return RoutingResult("2211", "loan_in", f"SC Balance Transfer disbursement (IBFT)", 95)
        if v := carriers.get("gxs_routing"):
            full = f"GXS:{v}:I-BANK"
            if hit := self.by_routing.get(full):
                return RoutingResult(hit[0], "loan_in", f"{hit[1]} (GXS)", 100)
        for k in ("dbs_cashline_routing",):
            if v := carriers.get(k):
                if hit := self.by_account.get(v):
                    side = "loan_in" if direction == "in" else "loan_pay"
                    return RoutingResult(hit[0], side, f"{hit[1]} (cashline ref)", 100)

        # ── 4. Recipient name match (PayNow) ──
        recipient = (carriers.get("paynow_recipient") or "").upper()
        if recipient:
            # Family/personal-name override before suspense — match longer prefix first
            for pat, coa, name in sorted(self.by_recipient, key=lambda x: -len(x[0])):
                if pat in recipient:
                    kind = "loan_pay" if str(coa).startswith("22") else \
                           "transfer" if str(coa).startswith("1") else "expense"
                    return RoutingResult(coa, kind, f"{name} (via PayNow {pat})", 90)

        # Entity-name uppercase carrier (SINGAPORE LIFE LTD etc.)
        entity = (carriers.get("entity_name_uppercase") or "").upper()
        if entity:
            for pat, coa, name in sorted(self.by_recipient, key=lambda x: -len(x[0])):
                if pat in entity:
                    return RoutingResult(coa, "expense", f"{name} (entity)", 90)
            # Vendor categories also try entity
            for coa, patterns in self.vendor_categories:
                for p in patterns:
                    if p in entity:
                        return RoutingResult(coa, "expense", f"Vendor pattern: {p}", 75)

        # ── 4b. Lifestyle-lump rules (user-stated 2026-05-14) ──
        # Only fire on OUTFLOWS. Inflows must NOT route to lifestyle (an expense),
        # otherwise deposits get treated as expense-refunds — the P&L net is
        # technically right but the categorisation is wrong.
        # See: feedback_lifestyle_expense_lumping.md
        LIFESTYLE = "5190"
        FAMILY = "5170"
        is_outflow = (direction == "out") or (deposit_amt <= 0 and amount > 0)
        if is_outflow:
            if "DEBIT CARD" in tx_type or "POINT-OF-SALE" in tx_type or "POINT OF SALE" in tx_type:
                return RoutingResult(LIFESTYLE, "expense", "Lifestyle (debit-card / POS)", 70)
            if "BILL PAYMENT" in tx_type and "DBS INTERNET" not in tx_type:
                return RoutingResult(LIFESTYLE, "expense", "Lifestyle (bill payment)", 70)
            if "FAST" in tx_type and (recipient or carriers.get("paynow_recipient")):
                return RoutingResult(LIFESTYLE, "expense", "Lifestyle (FAST/PayNow outflow, no known entity)", 65)
            if "CASH WITHDRAWAL" in tx_type or "ATM" in tx_type:
                return RoutingResult(FAMILY, "expense", "Family expense (cash withdrawal)", 65)
        # Cash deposit is always inflow direction
        if "CASH DEPOSIT MACHINE" in tx_type or "CASH DEPOSIT" in tx_type:
            return RoutingResult("1112", "transfer", "Cash deposit (cash on hand → POSB)", 75)

        # ── 5. tx_type-based markers (Salary / MEPS Receipt / Interest Earned) ──
        # Some carriers leak into tx_type when on header line (e.g. SC's IBFT|...).
        # Inspect tx_type directly for known patterns BEFORE generic markers.
        if "IBFT" in tx_type and ("DBSSSGSGBRT" in tx_type or "BRT" in tx_type):
            return RoutingResult("2211", "loan_in", "SC Balance Transfer disbursement (IBFT inline)", 95)
        if "INWARD CREDIT FEE" in tx_type:
            return RoutingResult("5700", "expense", "SC Inward Credit Fee", 90)
        if "TRANSFER WITHDRAWAL NTRF" in tx_type and "5498" in tx_type:
            return RoutingResult("2113", "cc_pay", "SC CC payment (inline TO CARD)", 95)
        if "FINANCE CHARGES" in tx_type or "FINANCE CHARGE" in tx_type:
            return RoutingResult("5700", "expense", "CC Finance charges", 90)
        if "MY PREFERRED PAYMENT PLAN" in tx_type or "MY PREF PMT PLN" in tx_type:
            # CC instalment plans — internal allocation, not new P&L
            return RoutingResult("1190", "transfer", "DBS CC instalment plan (internal)", 50)
        if "IL (60M)" in tx_type or "INSTL " in tx_type:
            return RoutingResult("1190", "transfer", "CC instalment loan tranche (internal)", 50)
        if "BILL PAYMENT - DBS INTERNET/WIRELESS" in tx_type:
            # CC payment received from another DBS/POSB account — likely already
            # cross-doc-deduped via transfer_pair ext_id. Still route to POSB hint.
            return RoutingResult("1111", "cc_pay", "DBS CC payment received (likely from POSB)", 85)

        for marker, (coa, name, kind) in self.by_tx_type.items():
            if marker in tx_type:
                conf = 80 if kind == "loan_in" else 70
                return RoutingResult(coa, kind, f"{name} (tx_type marker)", conf)

        # ── 6. Amount-pinpoint (e.g. EZ Loan $498.72) ──
        for ist_amt, coa, name, rec_req in self.by_instalment:
            if abs(amount - ist_amt) < 0.01:
                # If recipient required but missing, skip (avoid false positive)
                if rec_req and rec_req not in recipient and rec_req not in entity:
                    continue
                return RoutingResult(coa, "loan_pay", f"{name} (amount-pinpoint ${ist_amt})", 75)

        # ── 7. Vendor categories (last specific pass) ──
        # Try merchant_descriptor + paynow_recipient against expense patterns
        merchant = (carriers.get("merchant_descriptor") or "").upper()
        haystack = " ".join(filter(None, [recipient, entity, merchant, tx_type]))
        for coa, patterns in self.vendor_categories:
            for p in patterns:
                if p in haystack:
                    return RoutingResult(coa, "expense", f"Vendor pattern: {p}", 60)

        # ── 8. Generic income for unclassified inflows ──
        if deposit_amt > 0 or direction == "in":
            return RoutingResult("4900", "income", "Unclassified inflow (review)", 30)

        # ── 9. Suspense ──
        return RoutingResult(self.suspense_coa, "expense", f"Unclassified: {tx_type}", 0)


# Module-level singleton (loaded once per container start)
_router: Optional[AccountRouter] = None

def get_router() -> AccountRouter:
    global _router
    if _router is None:
        _router = AccountRouter()
    return _router


def reload_router():
    """Force a re-read of account_directory.yaml (for tests + admin edits)."""
    global _router
    _router = AccountRouter()
    return _router


if __name__ == "__main__":
    # CLI: route a single tx (passed as JSON via stdin) and print the result
    import json, sys
    r = get_router()
    tx_data = json.loads(sys.stdin.read())
    result = r.route(tx_data)
    print(json.dumps({
        "coa": result.coa, "kind": result.kind,
        "reason": result.reason, "confidence": result.confidence,
    }, indent=2))
