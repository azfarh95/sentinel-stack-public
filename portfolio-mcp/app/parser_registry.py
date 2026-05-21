"""Parser registry reader — joins bank_product_registry × parser_registry × statement_registry.

Answers bot queries:
  - "What banks does Sentinel know about?" → bank_product_registry
  - "What banks/products does Sentinel parse?" → parser_registry
  - "Does Sentinel parse OCBC credit cards?" → join + report
  - "What DBS products do we NOT parse?" → diff bank_product_registry vs parser_registry
  - "Show stats for SC CC parser" → parser_registry × statement_registry

Run:
    docker exec portfolio-mcp python -m app.parser_registry                 # supported parsers table
    docker exec portfolio-mcp python -m app.parser_registry --banks         # bank universe
    docker exec portfolio-mcp python -m app.parser_registry --gaps          # unsupported (bank, product)
    docker exec portfolio-mcp python -m app.parser_registry --slug sc       # one parser detail
    docker exec portfolio-mcp python -m app.parser_registry --check dbs cc  # is this (bank, product) supported?
    docker exec portfolio-mcp python -m app.parser_registry --json          # full structured dump
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml
from sqlalchemy import select, func

from . import database as db
from . import ledger

BANK_REGISTRY = Path("/finance/bank_product_registry.yaml")
PARSER_REGISTRY = Path("/finance/parser_registry.yaml")


# ── Loaders ─────────────────────────────────────────────────────────────────


def load_banks() -> dict:
    return yaml.safe_load(open(BANK_REGISTRY))


def load_parsers() -> dict:
    return yaml.safe_load(open(PARSER_REGISTRY))


def list_banks() -> list[dict]:
    return load_banks().get("banks", [])


def list_parsers() -> list[dict]:
    return load_parsers().get("parsers", [])


def get_bank(slug: str) -> dict | None:
    for b in list_banks():
        if b.get("slug") == slug:
            return b
    return None


def get_product(bank_slug: str, product_slug: str) -> dict | None:
    bank = get_bank(bank_slug)
    if not bank:
        return None
    for p in bank.get("products", []):
        if p.get("slug") == product_slug:
            return p
    return None


def get_parser(slug: str) -> dict | None:
    for p in list_parsers():
        if p.get("slug") == slug:
            return p
    return None


def get_parsers_for(bank_slug: str, product_slug: str) -> list[dict]:
    """Find all parser entries that handle this (bank, product)."""
    out = []
    for p in list_parsers():
        h = p.get("handles", {})
        if h.get("bank") == bank_slug and h.get("product") == product_slug:
            out.append(p)
            continue
        # also check sub_handles (e.g. SC parser handles both cc and balance_transfer)
        for sh in (p.get("sub_handles") or []):
            if sh.get("bank") == bank_slug and sh.get("product") == product_slug:
                out.append(p)
                break
    return out


# ── Coverage join ───────────────────────────────────────────────────────────


def coverage_for(slug: str, s=None) -> dict:
    """Lookup statement / payslip counts for a parser slug."""
    close = s is None
    s = s or db.SessionLocal()
    try:
        n, mn, mx = s.execute(
            select(func.count(ledger.StatementRegistry.id),
                   func.min(ledger.StatementRegistry.statement_date),
                   func.max(ledger.StatementRegistry.statement_date))
            .where(ledger.StatementRegistry.bank == slug)
        ).one()
        if n:
            return {"source": "statement_registry", "count": n,
                    "date_min": mn.isoformat() if mn else None,
                    "date_max": mx.isoformat() if mx else None}
        if slug.startswith("payslip_"):
            emp_key = slug.replace("payslip_", "")
            n, mn, mx = s.execute(
                select(func.count(ledger.PayslipRegistry.id),
                       func.min(ledger.PayslipRegistry.period_end),
                       func.max(ledger.PayslipRegistry.period_end))
                .where(ledger.PayslipRegistry.employer_key == emp_key)
            ).one()
            if n:
                return {"source": "payslip_registry", "count": n,
                        "date_min": mn.isoformat() if mn else None,
                        "date_max": mx.isoformat() if mx else None}
        return {"source": None, "count": 0, "date_min": None, "date_max": None}
    finally:
        if close:
            s.close()


# ── Renderers ───────────────────────────────────────────────────────────────


def render_parsers() -> None:
    parsers = list_parsers()
    s = db.SessionLocal()
    try:
        print(f"=== Sentinel Finance Parsers ({len(parsers)} formats) ===\n")
        print(f"  {'Slug':<24} {'Bank':<14} {'Product':<22} {'OCR':<4} {'Count':>5}  Coverage")
        print("  " + "-" * 100)
        for p in parsers:
            cov = coverage_for(p["slug"], s)
            h = p.get("handles", {})
            bank = h.get("bank") or h.get("employer") or "?"
            product = h.get("product") or h.get("doc_type") or "?"
            ocr = "yes" if p.get("requires_ocr") else "—"
            cov_str = f"{cov['date_min']} → {cov['date_max']}" if cov["count"] else "—"
            print(f"  {p['slug']:<24} {bank:<14} {product:<22} {ocr:<4} {cov['count']:>5}  {cov_str}")
    finally:
        s.close()


def render_banks() -> None:
    banks = list_banks()
    parsers = list_parsers()
    # Build set of (bank, product) tuples we parse
    covered = set()
    for p in parsers:
        h = p.get("handles", {})
        b, prod = h.get("bank"), h.get("product")
        if b and prod:
            covered.add((b, prod))
        for sh in (p.get("sub_handles") or []):
            covered.add((sh.get("bank"), sh.get("product")))

    print(f"=== Bank Universe ({len(banks)} banks) ===\n")
    print(f"  {'Bank':<22} {'Product':<30} {'Type':<18} {'Parsed?'}")
    print("  " + "-" * 90)
    total_prods = 0
    parsed_prods = 0
    for b in banks:
        bslug = b["slug"]
        for prod in b.get("products", []):
            total_prods += 1
            mark = "✓" if (bslug, prod["slug"]) in covered else "—"
            if mark == "✓":
                parsed_prods += 1
            print(f"  {b['display_name'][:21]:<22} {prod['display'][:29]:<30} "
                  f"{prod.get('type', '?'):<18} {mark}")
    print()
    print(f"  Coverage: {parsed_prods}/{total_prods} products parsed "
          f"({100 * parsed_prods / total_prods:.0f}%)")


def render_gaps() -> None:
    """Show (bank, product) tuples that exist in bank_product_registry but
    have NO parser. These are the next likely parsers to build."""
    banks = list_banks()
    parsers = list_parsers()
    covered = set()
    for p in parsers:
        h = p.get("handles", {})
        b, prod = h.get("bank"), h.get("product")
        if b and prod:
            covered.add((b, prod))
        for sh in (p.get("sub_handles") or []):
            covered.add((sh.get("bank"), sh.get("product")))

    print("=== Unsupported (bank, product) tuples ===\n")
    print(f"  {'Bank':<22} {'Product':<30} {'Type':<18}  Statement format")
    print("  " + "-" * 100)
    gaps = 0
    for b in banks:
        for prod in b.get("products", []):
            if (b["slug"], prod["slug"]) in covered:
                continue
            fmts = ", ".join(prod.get("statement_formats", []) or ["?"])
            print(f"  {b['display_name'][:21]:<22} {prod['display'][:29]:<30} "
                  f"{prod.get('type', '?'):<18}  {fmts}")
            gaps += 1
    print()
    print(f"  {gaps} unsupported (bank, product) combinations")


def render_parser_detail(slug: str) -> None:
    p = get_parser(slug)
    if not p:
        print(f"No parser with slug '{slug}'.")
        return
    h = p.get("handles", {})
    bank = get_bank(h.get("bank", "")) or {}
    product = get_product(h.get("bank", ""), h.get("product", "")) or {}
    s = db.SessionLocal()
    try:
        cov = coverage_for(slug, s)
    finally:
        s.close()
    print(f"=== Parser: {slug} ===\n")
    print(f"  Bank:         {bank.get('display_name', h.get('bank') or h.get('employer', '?'))}")
    print(f"  Product:      {product.get('display', h.get('product', '?'))}")
    print(f"  Type:         {product.get('type', '?')}")
    print(f"  Module:       {p.get('parser_module')}.{p.get('parser_function')}")
    print(f"  CoA Facility: {p.get('coa_facility')}")
    print(f"  OCR Required: {p.get('requires_ocr', False)}")
    print(f"  Coverage:     {cov['count']} samples ({cov['date_min']} → {cov['date_max']})")
    print()
    print(f"  Detection markers:")
    for m in p.get("detection_markers", []):
        print(f"    • {m}")
    for m in p.get("anti_markers", []) or []:
        print(f"    × {m}  (anti)")
    if p.get("filename_hints"):
        print(f"  Filename hints: {', '.join(p['filename_hints'])}")
    if p.get("field_anchors"):
        print(f"\n  Field anchors:")
        for k, v in p["field_anchors"].items():
            print(f"    {k:<18} {v}")
    if p.get("notes"):
        print(f"\n  Notes: {p['notes']}")


def check_supported(bank_slug: str, product_slug: str) -> None:
    """Answer 'is (bank, product) parsed?' with a clear yes/no + parser slug(s)."""
    bank = get_bank(bank_slug)
    if not bank:
        print(f"Unknown bank '{bank_slug}'. Run --banks to list.")
        return
    product = get_product(bank_slug, product_slug)
    if not product:
        prods = [p["slug"] for p in bank.get("products", [])]
        print(f"Bank '{bank['display_name']}' has no product '{product_slug}'.")
        print(f"  Known products: {', '.join(prods)}")
        return
    parsers = get_parsers_for(bank_slug, product_slug)
    print(f"=== {bank['display_name']} / {product['display']} ===\n")
    print(f"  Type:              {product.get('type', '?')}")
    print(f"  Statement formats: {product.get('statement_formats', [])}")
    if parsers:
        print(f"\n  ✓ SUPPORTED — {len(parsers)} parser(s):")
        s = db.SessionLocal()
        try:
            for p in parsers:
                cov = coverage_for(p["slug"], s)
                print(f"    - {p['slug']:<22} ({p['parser_module']}.{p['parser_function']})  "
                      f"{cov['count']} samples")
        finally:
            s.close()
    else:
        print(f"\n  ✗ UNSUPPORTED — no parser yet for this (bank, product) tuple.")
        print(f"  To add a parser, see parser_registry.yaml maintainer_notes.")


# ── CLI ─────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="Show one parser's detail")
    ap.add_argument("--banks", action="store_true",
                    help="Show bank universe + per-product parser coverage")
    ap.add_argument("--gaps", action="store_true",
                    help="Show (bank, product) tuples with no parser")
    ap.add_argument("--check", nargs=2, metavar=("BANK", "PRODUCT"),
                    help="Check if a (bank, product) is supported. e.g. --check dbs cc")
    ap.add_argument("--json", action="store_true",
                    help="Dump full structured registry (incl. coverage) as JSON")
    args = ap.parse_args()

    if args.json:
        cfg = {"banks": load_banks(), "parsers": load_parsers()}
        s = db.SessionLocal()
        try:
            for p in cfg["parsers"].get("parsers", []):
                p["_coverage"] = coverage_for(p["slug"], s)
        finally:
            s.close()
        print(json.dumps(cfg, indent=2, default=str))
    elif args.check:
        check_supported(args.check[0], args.check[1])
    elif args.banks:
        render_banks()
    elif args.gaps:
        render_gaps()
    elif args.slug:
        render_parser_detail(args.slug)
    else:
        render_parsers()


if __name__ == "__main__":
    main()
