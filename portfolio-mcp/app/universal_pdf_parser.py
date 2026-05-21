"""Universal PDF parser — schema-driven extraction.

Replaces N bank-specific parsers with one engine that reads YAML schemas from
`finance/statement_schemas/` (also mirrored at `/finance/statement_schemas/`
inside the container).

Built on the Phase-0 schema work (commit 707e77e in YOUR_GITHUB_USERNAME/sentinel-finance).
The schemas capture exactly the per-bank fields Firefly's PDF importer was
discarding — recipient names, policy refs, bank routing codes, card numbers —
so that downstream classifiers never have to guess again.

Usage:
    docker exec portfolio-mcp python -m app.universal_pdf_parser \\
        --file "/onedrive/.../Deposit Account Statement_Apr2026.pdf"
    docker exec portfolio-mcp python -m app.universal_pdf_parser \\
        --folder "/onedrive/Sentinel Finance/01_Bank statements/DBS_POSB Savings"

Output: JSON to stdout. One object per statement with:
    {
      "schema":      "posb-savings",
      "bank":        "POSB",
      "product":     "POSB Savings / Deposit Account",
      "account":     "170-37376-6",
      "statement_date": "2026-04-30",
      "currency":    "SGD",
      "balance_brought_forward": 1234.56,
      "balance_carried_forward": 89.29,
      "transactions": [
        {
          "date":         "2026-04-13",
          "type":         "Payments / Collections via GIRO",
          "amount":       252.85,
          "direction":    "out",   # "in" | "out"
          "raw_lines":    ["13 Apr Payments / Collections via GIRO 252.85",
                           "SINGAPORE LIFE LTD",
                           "P4064051170373766",
                           "P4064051"],
          "carriers": {
            "entity_name_uppercase":   "SINGAPORE LIFE LTD",
            "insurance_policy_ref":    "P4064051",
            "insurance_policy_long_ref": "P4064051170373766"
          }
        },
        ...
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Where schemas live (mounted via docker-compose volume on /finance, falls back to repo root)
SCHEMA_DIRS = [
    Path("/finance/statement_schemas"),       # primary mount
    Path("/app/finance/statement_schemas"),
    Path(__file__).parent.parent / "finance" / "statement_schemas",
]


@dataclass
class Transaction:
    """A parsed transaction. Direction is NOT inferred — it's derived from the
    column position the amount sits in on the source PDF. For multi-column
    statements (POSB-style), `withdrawal_amount` and `deposit_amount` are
    separately populated. For single-column statements (DBS CC etc.) where
    every amount is a debit unless suffixed CR, use `amount` + the schema's
    `amount_sign` rule."""
    date_str: str                          # raw date as captured
    date_iso: Optional[str] = None         # YYYY-MM-DD if parsed
    tx_type: str = ""
    amount: float = 0.0                    # absolute amount (for legacy reads)
    withdrawal_amount: float = 0.0         # populated when amount is in WITHDRAWAL column
    deposit_amount: float = 0.0            # populated when amount is in DEPOSIT column
    direction: str = "unknown"             # "in" | "out" | "unknown" (kept for callers; derived from columns)
    running_balance: Optional[float] = None
    raw_lines: list[str] = field(default_factory=list)
    carriers: dict[str, str] = field(default_factory=dict)


@dataclass
class StatementExtract:
    schema_name: str
    bank: str
    product: str
    gl_account_code: Optional[str]
    account: Optional[str] = None
    statement_date: Optional[str] = None
    currency: str = "SGD"
    balance_brought_forward: Optional[float] = None
    balance_carried_forward: Optional[float] = None
    transactions: list[Transaction] = field(default_factory=list)
    source_path: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def find_schema_dir() -> Path:
    for d in SCHEMA_DIRS:
        if d.exists() and any(d.glob("*.yaml")):
            return d
    raise FileNotFoundError(f"No schema dir with .yaml found in {SCHEMA_DIRS}")


def load_all_schemas() -> list[dict]:
    """Load every schema in the directory. Returns list of dicts with __filename__ key."""
    d = find_schema_dir()
    schemas = []
    for f in sorted(d.glob("*.yaml")):
        if f.name == "_index.yaml":
            continue
        with open(f, "r", encoding="utf-8") as fp:
            s = yaml.safe_load(fp) or {}
        s["__filename__"] = f.name
        schemas.append(s)
    return schemas


def pick_schema(pdf_path: Path, all_schemas: list[dict], probe_text: str = "") -> Optional[dict]:
    """Match PDF to schema by filename pattern + (optional) header marker."""
    name = pdf_path.name
    candidates = []
    for s in all_schemas:
        for pat in s.get("filename_patterns") or []:
            if re.search(pat, name, re.IGNORECASE):
                # Secondary check: header marker if probe text supplied
                if probe_text:
                    markers = s.get("header_markers") or []
                    if markers and not any(re.search(re.escape(m), probe_text, re.IGNORECASE) for m in markers):
                        continue
                candidates.append(s)
                break
    if not candidates:
        return None
    # Most specific = longest filename pattern wins
    return max(candidates, key=lambda s: max(len(p) for p in s.get("filename_patterns", [""])))


def extract_text(pdf_path: Path, requires_ocr: bool = False, max_pages: int = 50) -> str:
    """Extract text via ocr_normalize cache (universal). Joins per-page text.
    `requires_ocr` parameter retained for backward compat — ignored, since
    ocr_normalize auto-detects text-PDF vs image-PDF."""
    try:
        from app.ocr_normalize import normalize
        cached = normalize(Path(pdf_path))
        pages = cached.get("pages", [])[:max_pages]
        return "\n".join((p.get("text") or "") for p in pages)
    except Exception as e:
        logger.error(f"ocr_normalize failed on {pdf_path}: {e}")
        return ""


class _NormalizedPage:
    """pdfplumber-compatible page shim backed by ocr_normalize cache."""
    def __init__(self, page_dict: dict):
        self._page = page_dict
        self.width = page_dict.get("width", 0.0)
        self.height = page_dict.get("height", 0.0)

    def extract_words(self, **_kwargs) -> list[dict]:
        """Return word list in pdfplumber format ({text, x0, x1, top, bottom})."""
        return [
            {
                "text": w["text"],
                "x0": w["x0"], "x1": w["x1"],
                "top": w.get("y0", 0.0),
                "bottom": w.get("y1", 0.0),
                "doctop": w.get("y0", 0.0),
            }
            for w in self._page.get("words", [])
        ]

    def extract_text(self, **_kwargs) -> str:
        return self._page.get("text", "")


class _NormalizedPdf:
    """pdfplumber-compatible document shim backed by ocr_normalize cache."""
    def __init__(self, pages: list[dict]):
        self.pages = [_NormalizedPage(p) for p in pages]

    def __enter__(self): return self
    def __exit__(self, *_): return None


def _open_normalized(pdf_path):
    """Drop-in replacement for `pdfplumber.open(...)`. Reads from OCR cache."""
    from app.ocr_normalize import normalize
    cached = normalize(Path(pdf_path))
    return _NormalizedPdf(cached.get("pages", []))


def parse_amount(s: str) -> float:
    """'1,234.56' → 1234.56"""
    return float(s.replace(",", "").replace("$", "").strip())


def first_match(pattern: str, text: str, flags=re.MULTILINE):
    """Return first regex group(1) match or None."""
    if not pattern:
        return None
    m = re.search(pattern, text, flags)
    if not m:
        return None
    try:
        return m.group(1)
    except IndexError:
        return m.group(0)


def parse_balance_anchors(schema: dict, text: str) -> dict:
    """Extract BF/CF/Opening/Closing balances per schema.

    For multi-page statements: BF is taken from the FIRST match (true opening),
    CF is taken from the LAST match (true closing — multiple CF anchors appear
    per page in POSB, only the very last one is the actual month-end balance).
    """
    anchors = schema.get("balance_anchors", {}) or {}
    out = {}
    LAST_MATCH_KEYS = {"carried_forward", "closing"}
    for key, pat in anchors.items():
        matches = re.findall(pat, text, re.MULTILINE)
        if not matches:
            continue
        # Pick first by default; last for end-of-statement anchors
        v = matches[-1] if key in LAST_MATCH_KEYS else matches[0]
        # If group capture returned a tuple, take the first non-empty
        if isinstance(v, tuple):
            v = next((x for x in v if x), v[0])
        try:
            out[key] = parse_amount(v)
        except ValueError:
            pass
    return out


def parse_account_number(schema: dict, text: str) -> Optional[str]:
    spec = schema.get("account_number")
    if isinstance(spec, dict):
        return first_match(spec.get("regex", ""), text)
    return None


def parse_statement_date(schema: dict, text: str) -> Optional[str]:
    spec = schema.get("statement_date")
    if not isinstance(spec, dict):
        return None
    raw = first_match(spec.get("regex", ""), text)
    if not raw:
        return None
    fmt = spec.get("format")
    if fmt:
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            return raw
    return raw


# ── POSB-style multi-line tx parsing ──────────────────────────────────────
# This is the workhorse: a tx HEADER line (date + type + amount) followed by
# 0-5 continuation lines until the next header OR a balance-anchor line.

def parse_multiline_transactions(schema: dict, text: str, statement_year: Optional[int] = None,
                                 amount_cols: Optional[dict] = None) -> list[Transaction]:
    """Parse transactions from extracted text.

    If `amount_cols` is provided (output of extract_amount_columns), use column
    position to deterministically set withdrawal_amount vs deposit_amount.
    Otherwise consult schema's `tx_table.amount_sign` rule:
      - suffix_CR: credit  → tx ending with " CR" = deposit (payment in)
      - suffix_OD: debit_balance → tx ending with "OD" = debit-balance marker (UOB)
      - trailing_dash: charge → tx ending with "-" = charge/deduction (Singlife)
    """
    amount_cols = amount_cols or {}
    # Read amount_sign convention from schema (governs CR/OD/trailing-dash interpretation)
    amount_sign = (schema.get("tx_table") or {}).get("amount_sign") or {}
    tx_header_pat = schema.get("tx_header_regex")
    if not tx_header_pat:
        return []
    tx_types = schema.get("tx_types") or []
    exclude_lines = schema.get("exclude_lines") or []
    exclude_compiled = [re.compile(p) for p in exclude_lines]
    date_format = (schema.get("tx_table") or {}).get("date_format")

    # OCR text-cleanup pre-pass (per-schema). Applied before line-by-line regex
    # matching to fix common tesseract letter-confusions at the schema's font/dpi.
    for rule in schema.get("ocr_text_cleanup") or []:
        text = re.sub(rule["pattern"], rule["replacement"], text, flags=re.MULTILINE)

    lines = text.split("\n")
    transactions: list[Transaction] = []
    cur: Optional[Transaction] = None
    running_balance_before: Optional[float] = None

    header_re = re.compile(tx_header_pat)
    bf_re = re.compile(r"Balance Brought Forward\s+([\d,]+\.\d{2})", re.IGNORECASE)
    cf_re = re.compile(r"Balance Carried Forward\s+([\d,]+\.\d{2})", re.IGNORECASE)

    def is_excluded(line: str) -> bool:
        for pat in exclude_compiled:
            if pat.search(line):
                return True
        return False

    def flush():
        nonlocal cur
        if cur:
            transactions.append(cur)
            cur = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or is_excluded(line):
            continue

        # Track running balance from BF/CF anchors for direction inference
        m_bf = bf_re.search(line)
        if m_bf:
            flush()
            running_balance_before = parse_amount(m_bf.group(1))
            continue
        m_cf = cf_re.search(line)
        if m_cf:
            flush()
            continue

        m = header_re.match(line)
        # Validate that the matched "type" is in tx_types (avoid false positives
        # on lines like merchant descriptors that happen to start with digits)
        if m and tx_types:
            try:
                ty = m.group(3) if m.lastindex and m.lastindex >= 3 else None
                if ty and not any(t.lower() in ty.lower() or ty.lower() in t.lower() for t in tx_types):
                    m = None
            except IndexError:
                pass

        if m:
            flush()
            grps = m.groups()
            day = grps[0] if len(grps) > 0 else ""
            month = grps[1] if len(grps) > 1 else ""
            ty = grps[2] if len(grps) > 2 else ""
            amt = grps[3] if len(grps) > 3 else ""
            running_bal = grps[4] if len(grps) > 4 else None

            # Build ISO date
            date_iso = None
            if day and month and statement_year:
                try:
                    date_iso = datetime.strptime(
                        f"{day} {month} {statement_year}", "%d %b %Y"
                    ).date().isoformat()
                except ValueError:
                    pass

            try:
                amt_f = parse_amount(amt) if amt else 0.0
            except ValueError:
                amt_f = 0.0
            running_bal_f = None
            if running_bal:
                try:
                    running_bal_f = parse_amount(running_bal)
                except ValueError:
                    pass

            # Direction: PURE COLUMN-POSITION extraction (no heuristics).
            # amount_cols was built by extract_amount_columns() from pdfplumber word
            # boundaries; it tells us whether amt_f's instance on this line sits in
            # the WITHDRAWAL($) or DEPOSIT($) column.
            withdrawal_amt = 0.0
            deposit_amt = 0.0
            direction = "unknown"
            # ── Schema amount_sign rules (CC / CashLine / ILP single-column docs) ──
            # If `suffix_CR: credit` is set, lines ending " CR" are deposits (payments in).
            # For CC: deposit = payment received (liability down); withdrawal = charge.
            line_upper = line.upper().rstrip()
            suffix_cr = amount_sign.get("suffix_CR")
            suffix_od = amount_sign.get("suffix_OD")
            trailing_dash = amount_sign.get("trailing_dash")
            default_sign = amount_sign.get("default")
            # Accept "CR" with or without preceding space — HSBC OCR yields "205.00CR"
            if suffix_cr and (line_upper.endswith(" CR") or line_upper.endswith("CR")):
                # Payment/credit received — for CC, this is liability-reducing (a deposit)
                if suffix_cr == "credit":
                    deposit_amt = amt_f
                    direction = "in"
            elif suffix_od and line_upper.endswith("OD"):
                # OD = overdraft marker (UOB CashPlus running balance only — not a tx amount)
                pass  # leave both as 0; handled separately
            elif trailing_dash and line.rstrip().endswith("-"):
                if trailing_dash == "charge":
                    withdrawal_amt = amt_f
                    direction = "out"
            elif default_sign == "debit" and amt_f > 0:
                # Default for CC: amount with no CR suffix = charge (liability up)
                withdrawal_amt = amt_f
                direction = "out"

            if amount_cols and direction == "unknown":
                amt_key_norm = f"{amt_f:.2f}"
                # Look for any column entry matching this value on any y-row close to
                # this line. Without exact y-coords from text-flow, we accept any.
                matches = [v for (y, val), v in amount_cols.items() if val == amt_key_norm]
                # If running balance also present, it's a deposit-or-withdrawal + balance.
                # In that case, the FIRST (non-balance) entry tells us direction.
                non_balance = [v for v in matches if v != "balance"]
                if non_balance:
                    col = non_balance[0]
                    if col == "withdrawal":
                        withdrawal_amt = amt_f; direction = "out"
                    elif col == "deposit":
                        deposit_amt = amt_f; direction = "in"
            # Running-balance fallback (when column lookup failed)
            if direction == "unknown":
                if running_bal_f is not None and running_balance_before is not None:
                    if abs((running_bal_f - running_balance_before) - amt_f) < 0.01:
                        direction = "in"; deposit_amt = amt_f
                    elif abs((running_balance_before - running_bal_f) - amt_f) < 0.01:
                        direction = "out"; withdrawal_amt = amt_f

            if running_bal_f is not None:
                running_balance_before = running_bal_f

            cur = Transaction(
                date_str=f"{day} {month}",
                date_iso=date_iso,
                tx_type=ty.strip(),
                amount=amt_f,
                withdrawal_amount=withdrawal_amt,
                deposit_amount=deposit_amt,
                direction=direction,
                running_balance=running_bal_f,
                raw_lines=[line],
            )
        elif cur is not None:
            # Continuation line — collect until next header
            cur.raw_lines.append(line)
    flush()

    # Post-process: apply known_carriers + direction refinement
    cont_spec = schema.get("continuation_lines") or {}
    carriers_spec = cont_spec.get("known_carriers") or []
    INFLOW_HINTS = ("Incoming PayNow", "Inward FAST", "From: ", "Incoming IBG",
                    "Fund Transfer\n6,", "GRABPAY TOPUP")
    OUTFLOW_HINTS = ("To: ", "PayNow Transfer", "Transfer to",
                     "OTHR Other", "OTHR Transfer")
    for tx in transactions:
        # Direction refinement from continuation lines (only if tx_type-based was inconclusive)
        joined_cont = "\n".join(tx.raw_lines[1:])
        if any(h in joined_cont for h in INFLOW_HINTS):
            tx.direction = "in"
        elif any(h in joined_cont for h in OUTFLOW_HINTS):
            # Don't override "in" if tx_type already strongly set it (e.g. Salary)
            ty_upper = (tx.tx_type or "").upper()
            STRONG_INFLOW = ("SALARY", "INTEREST EARNED", "FAST COLLECTION",
                             "INCOMING PAYNOW", "INWARD FAST", "INCOMING IBG")
            if not any(s in ty_upper for s in STRONG_INFLOW):
                tx.direction = "out"
        for sub_line in tx.raw_lines[1:]:
            for carrier in carriers_spec:
                kind = carrier.get("kind", "unknown")
                pat = carrier.get("pattern", "")
                if not pat:
                    continue
                m = re.match(pat, sub_line)
                if m:
                    # Prefer the LAST capture group (usually the meaningful value);
                    # fall back to group 1 if only one; then group 0 (whole match).
                    val = None
                    if m.lastindex:
                        val = m.group(m.lastindex)
                    if not val:
                        try: val = m.group(1)
                        except IndexError: pass
                    if not val:
                        val = m.group(0)
                    tx.carriers[kind] = val
                    break
    return transactions


def parse_by_word_rows(pdf_path: Path, schema: dict, statement_year: Optional[int] = None) -> list["Transaction"]:
    """Parse transactions DIRECTLY from word-positions (no text-line ambiguity).

    For each PDF page:
      1. Extract words with bounding boxes
      2. Find column-header positions (WITHDRAWAL($), DEPOSIT($), BALANCE($))
      3. Group words into rows (tight y-tolerance)
      4. For each row, find amount-words and classify by nearest column-header
      5. If row starts with a date-pattern word + tx-type → it's a tx header row
      6. Subsequent rows without date prefix are continuation lines
      7. Build Transaction with deterministic withdrawal_amount/deposit_amount
         from column-classified amount tokens. NO INFERENCE.
    """
    import re as _re
    from collections import defaultdict
    transactions: list[Transaction] = []
    if not statement_year:
        statement_year = datetime.now().year
    cur: Optional[Transaction] = None
    last_balance: Optional[float] = None

    AMOUNT_RE = _re.compile(r"^[\d,]+\.\d{2}$")
    DATE_RE = _re.compile(r"^\d{1,2}$")
    MONTH_RE = _re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$")
    tx_types = set(t.lower() for t in schema.get("tx_types") or [])
    # Anchor patterns (BF/CF/Opening/Closing) — these rows are NOT transactions
    ANCHOR_PATTERNS = [
        _re.compile(r"Balance\s+Brought\s+Forward", _re.IGNORECASE),
        _re.compile(r"Balance\s+Carried\s+Forward", _re.IGNORECASE),
        _re.compile(r"Opening\s+Balance", _re.IGNORECASE),
        _re.compile(r"Closing\s+Balance", _re.IGNORECASE),
        _re.compile(r"Balance\s+from\s+Previous\s+Statement", _re.IGNORECASE),
        _re.compile(r"Total\s+\d+\.\d{2}", _re.IGNORECASE),   # "Total 352.00 350.00"
    ]

    def flush():
        nonlocal cur
        if cur:
            transactions.append(cur)
            cur = None

    with _open_normalized(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False) or []
            if not words:
                continue
            # Find column header positions — require all 3 on the SAME y-row to
            # avoid matching "Balance Brought Forward" caption as the BALANCE header.
            # Group words by approximate y, find a row that has WITHDRAWAL+DEPOSIT+BALANCE.
            row_groups = defaultdict(list)
            for w in words:
                row_groups[round(w["top"] / 2) * 2].append(w)
            wd_c = dp_c = bl_c = None
            for yk in sorted(row_groups.keys()):
                row_text_upper = " ".join(w["text"].upper() for w in row_groups[yk])
                if "WITHDRAWAL" in row_text_upper and "DEPOSIT" in row_text_upper and "BALANCE" in row_text_upper:
                    # Found the column-header row. Pick precise X centers.
                    for w in row_groups[yk]:
                        t = w["text"].upper()
                        if t.startswith("WITHDRAWAL"): wd_c = (w["x0"] + w["x1"]) / 2
                        elif t.startswith("DEPOSIT"):  dp_c = (w["x0"] + w["x1"]) / 2
                        elif t.startswith("BALANCE"):  bl_c = (w["x0"] + w["x1"]) / 2
                    break
            if not (wd_c and dp_c and bl_c):
                continue   # not a POSB-style multi-column page

            # Group words into rows (y-tolerance 3pt)
            rows = defaultdict(list)
            for w in words:
                rk = round(w["top"] / 3) * 3
                rows[rk].append(w)
            # Look for BF / CF anchors first to set last_balance
            for rk in sorted(rows.keys()):
                row_text = " ".join(w["text"] for w in sorted(rows[rk], key=lambda x: x["x0"]))
                m_bf = _re.search(r"Balance Brought Forward\s+([\d,]+\.\d{2})", row_text)
                if m_bf:
                    flush()
                    try: last_balance = parse_amount(m_bf.group(1))
                    except: pass
                    continue
                m_cf = _re.search(r"Balance Carried Forward\s+([\d,]+\.\d{2})", row_text)
                if m_cf:
                    flush()
                    continue

                # Sort words left-to-right
                row_words = sorted(rows[rk], key=lambda x: x["x0"])
                # Extract amounts + their column
                amts = []
                for w in row_words:
                    if AMOUNT_RE.match(w["text"]):
                        xc = (w["x0"] + w["x1"]) / 2
                        d_wd = abs(xc - wd_c); d_dp = abs(xc - dp_c); d_bl = abs(xc - bl_c)
                        if d_bl == min(d_wd, d_dp, d_bl):    col = "balance"
                        elif d_wd == min(d_wd, d_dp):         col = "withdrawal"
                        else:                                  col = "deposit"
                        amts.append({"val": parse_amount(w["text"]), "col": col, "raw": w["text"]})
                # Skip anchor rows (BF / CF / Opening / Closing / Total)
                if any(p.search(row_text) for p in ANCHOR_PATTERNS):
                    flush()
                    # Try to extract balance value from the row if BF/Opening
                    for w in row_words:
                        if AMOUNT_RE.match(w["text"]):
                            try:
                                v = parse_amount(w["text"])
                                if any(p2.search(row_text) for p2 in ANCHOR_PATTERNS[:1]+ANCHOR_PATTERNS[2:3]+ANCHOR_PATTERNS[4:5]):
                                    last_balance = v
                            except: pass
                            break
                    continue
                # Is this a tx-header row? Heuristic: first 2 words are a date (DD Month)
                if len(row_words) >= 3:
                    w0, w1 = row_words[0], row_words[1]
                    if DATE_RE.match(w0["text"]) and MONTH_RE.match(w1["text"]):
                        # Tx type = words between w1 and the first amount
                        rest_after_date = row_words[2:]
                        type_words = []
                        for rw in rest_after_date:
                            if AMOUNT_RE.match(rw["text"]):
                                break
                            type_words.append(rw["text"])
                        tx_type_str = " ".join(type_words)
                        # Validate against schema's tx_types if provided
                        if tx_types and not any(t in tx_type_str.lower() for t in tx_types):
                            # Not a recognized tx header; treat as continuation
                            if cur is not None:
                                cur.raw_lines.append(" ".join(w["text"] for w in row_words))
                            continue
                        flush()
                        # Build tx from amounts
                        withdrawal_amt = 0.0
                        deposit_amt = 0.0
                        running_bal = None
                        primary_amt = 0.0
                        for a in amts:
                            if a["col"] == "withdrawal":
                                withdrawal_amt = a["val"]
                                primary_amt = a["val"]
                            elif a["col"] == "deposit":
                                deposit_amt = a["val"]
                                primary_amt = a["val"]
                            elif a["col"] == "balance":
                                running_bal = a["val"]
                        # Direction is now KNOWN
                        if withdrawal_amt > 0:    direction = "out"
                        elif deposit_amt > 0:     direction = "in"
                        else:                     direction = "unknown"
                        # Build ISO date
                        date_iso = None
                        try:
                            date_iso = datetime.strptime(
                                f"{w0['text']} {w1['text']} {statement_year}", "%d %b %Y"
                            ).date().isoformat()
                        except ValueError:
                            pass
                        cur = Transaction(
                            date_str=f"{w0['text']} {w1['text']}",
                            date_iso=date_iso,
                            tx_type=tx_type_str,
                            amount=primary_amt,
                            withdrawal_amount=withdrawal_amt,
                            deposit_amount=deposit_amt,
                            direction=direction,
                            running_balance=running_bal,
                            raw_lines=[" ".join(w["text"] for w in row_words)],
                        )
                        if running_bal is not None:
                            last_balance = running_bal
                        continue
                # Continuation line for current tx
                if cur is not None:
                    cur.raw_lines.append(" ".join(w["text"] for w in row_words))
    flush()

    # Post-process: apply carriers
    cont_spec = schema.get("continuation_lines") or {}
    carriers_spec = cont_spec.get("known_carriers") or []
    import re as _re2
    for tx in transactions:
        for sub_line in tx.raw_lines[1:]:
            for carrier in carriers_spec:
                kind = carrier.get("kind", "unknown")
                pat = carrier.get("pattern", "")
                if not pat: continue
                m = _re2.match(pat, sub_line)
                if m:
                    val = None
                    if m.lastindex: val = m.group(m.lastindex)
                    if not val:
                        try: val = m.group(1)
                        except IndexError: pass
                    if not val: val = m.group(0)
                    tx.carriers[kind] = val
                    break
    return transactions


def extract_amount_columns(pdf_path: Path) -> dict:
    """For POSB-style multi-column statements, scan each amount-bearing word and
    determine if it's in the WITHDRAWAL, DEPOSIT, or BALANCE column.

    Returns: {(round(y), round(x1)): "withdrawal" | "deposit" | "balance" | "unknown"}
    Caller looks up the tx amount position to infer direction.

    For POSB Deposit Account Statement format (columns approximately):
      WITHDRAWAL($) right-edge ~390-420
      DEPOSIT($)    right-edge ~470-500
      BALANCE($)    right-edge ~555-575
    """
    from collections import defaultdict
    import re as _re

    out = {}
    with _open_normalized(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False) or []
            # Find column header positions — require all 3 on the same y-row
            row_groups = defaultdict(list)
            for w in words:
                row_groups[round(w["top"] / 2) * 2].append(w)
            wd_x = dp_x = bl_x = None
            for yk in sorted(row_groups.keys()):
                row_text_upper = " ".join(w["text"].upper() for w in row_groups[yk])
                if "WITHDRAWAL" in row_text_upper and "DEPOSIT" in row_text_upper and "BALANCE" in row_text_upper:
                    for w in row_groups[yk]:
                        t = w["text"].upper()
                        if t.startswith("WITHDRAWAL"): wd_x = (w["x0"], w["x1"])
                        elif t.startswith("DEPOSIT"):  dp_x = (w["x0"], w["x1"])
                        elif t.startswith("BALANCE"):  bl_x = (w["x0"], w["x1"])
                    break
            if not (wd_x and dp_x and bl_x):
                continue

            # For each amount-looking word, classify by which column its center sits in
            for w in words:
                if not _re.match(r"^[\d,]+\.\d{2}$", w["text"]):
                    continue
                xc = (w["x0"] + w["x1"]) / 2
                # Use header centers as anchors
                wd_c = (wd_x[0] + wd_x[1]) / 2
                dp_c = (dp_x[0] + dp_x[1]) / 2
                bl_c = (bl_x[0] + bl_x[1]) / 2
                # Closest header
                d_wd = abs(xc - wd_c)
                d_dp = abs(xc - dp_c)
                d_bl = abs(xc - bl_c)
                col = "withdrawal" if d_wd == min(d_wd, d_dp, d_bl) else \
                      "deposit" if d_dp == min(d_wd, d_dp, d_bl) else "balance"
                # Key by approximate (y, value) so the tx-parsing pass can look up
                key = (round(w["top"]), w["text"].replace(",", ""))
                out[key] = col
    return out


def parse_pdf(pdf_path: Path, all_schemas: list[dict]) -> StatementExtract:
    # Initial probe for text + schema selection
    raw_text = extract_text(pdf_path, requires_ocr=False, max_pages=2)
    schema = pick_schema(pdf_path, all_schemas, probe_text=raw_text)
    if not schema:
        return StatementExtract(
            schema_name="UNKNOWN", bank="?", product="?",
            gl_account_code=None, source_path=str(pdf_path),
            warnings=[f"No schema matched filename {pdf_path.name}"],
        )

    # Full extraction now that we know the schema
    requires_ocr = schema.get("requires_ocr", False)
    text = extract_text(pdf_path, requires_ocr=requires_ocr, max_pages=50)

    # For multi-column statements (POSB / Maybank Savings with WITHDRAWAL+DEPOSIT
    # columns), use word-position parser directly — no inference.
    use_word_row_parser = False
    if not requires_ocr and schema.get("tx_table", {}).get("columns"):
        cols = [c.upper() for c in schema["tx_table"]["columns"]]
        if "WITHDRAWAL" in " ".join(cols) and "DEPOSIT" in " ".join(cols):
            use_word_row_parser = True
    amount_cols = {}

    stmt = StatementExtract(
        schema_name=schema["__filename__"].replace(".yaml", ""),
        bank=schema.get("bank", "?"),
        product=schema.get("product", "?"),
        gl_account_code=schema.get("gl_account_code"),
        currency=schema.get("currency", "SGD"),
        source_path=str(pdf_path),
    )
    stmt.account = parse_account_number(schema, text)
    stmt.statement_date = parse_statement_date(schema, text)
    anchors = parse_balance_anchors(schema, text)
    stmt.balance_brought_forward = anchors.get("brought_forward") or anchors.get("opening") or anchors.get("previous")
    stmt.balance_carried_forward = anchors.get("carried_forward") or anchors.get("closing")

    # Derive statement year from statement_date for tx date inference
    year = None
    if stmt.statement_date:
        try:
            year = datetime.fromisoformat(stmt.statement_date).year
        except ValueError:
            m = re.search(r"\b(20\d{2})\b", stmt.statement_date)
            if m:
                year = int(m.group(1))

    if use_word_row_parser:
        stmt.transactions = parse_by_word_rows(pdf_path, schema, statement_year=year)
    else:
        stmt.transactions = parse_multiline_transactions(
            schema, text, statement_year=year, amount_cols=amount_cols
        )
    return stmt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="Single PDF to parse")
    ap.add_argument("--folder", help="Folder of PDFs to parse")
    ap.add_argument("--summary", action="store_true", help="Print summary table instead of full JSON")
    args = ap.parse_args()

    schemas = load_all_schemas()
    print(f"# Loaded {len(schemas)} schemas from {find_schema_dir()}", file=sys.stderr)

    if args.file:
        pdfs = [Path(args.file)]
    elif args.folder:
        pdfs = sorted(Path(args.folder).glob("*.pdf"))
    else:
        ap.error("Either --file or --folder required")
        return

    results = []
    for p in pdfs:
        try:
            r = parse_pdf(p, schemas)
            results.append(r)
        except Exception as e:
            print(f"# ERR {p.name}: {e}", file=sys.stderr)

    if args.summary:
        print(f"{'schema':<22} {'date':<12} {'BF':>12} {'CF':>12} {'tx':>5}  {'file':<40}")
        print("-" * 110)
        for r in results:
            bf = f"{r.balance_brought_forward:>12,.2f}" if r.balance_brought_forward else "—".rjust(12)
            cf = f"{r.balance_carried_forward:>12,.2f}" if r.balance_carried_forward else "—".rjust(12)
            print(f"{r.schema_name:<22} {(r.statement_date or '-'):<12} {bf} {cf} {len(r.transactions):>5}  {Path(r.source_path).name[:40]}")
    else:
        print(json.dumps([r.to_dict() for r in results], default=str, indent=2))


if __name__ == "__main__":
    main()
