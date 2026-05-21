"""Inbox orchestrator — single drop zone, classify + route + optionally journal.

User dumps any file into /onedrive/Sentinel Finance/_INBOX/. This module:
  1. Walks _INBOX/
  2. Classifies each file via doc_classifier.classify()
  3. Moves to category-specific pile folder (canonical-named if applicable)
  4. With --post: dispatches to pile-specific parser and posts journal(s) via
     journal_service.post_journal() (idempotent via external_id)
  5. Unknowns (confidence < threshold) → _QUEUE/ for manual review
  6. Logs every action to /data/inbox_pipeline.log

Run:
    docker exec portfolio-mcp python -m app.inbox_pipeline                   # dry-run
    docker exec portfolio-mcp python -m app.inbox_pipeline --apply           # move only
    docker exec portfolio-mcp python -m app.inbox_pipeline --apply --post    # move + parse + journal
    docker exec portfolio-mcp python -m app.inbox_pipeline --apply --post --watch  # daemon
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import doc_classifier as dc

logger = logging.getLogger(__name__)

INBOX = Path("/onedrive/Sentinel Finance/_INBOX")
QUEUE = Path("/onedrive/Sentinel Finance/_QUEUE")
LOG_PATH = Path("/data/inbox_pipeline.log")


def ensure_dirs() -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    QUEUE.mkdir(parents=True, exist_ok=True)


def collision_safe(dst: Path) -> Path:
    """If dst exists, suffix with ' (1)', ' (2)', ..."""
    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    i = 1
    while True:
        candidate = dst.parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


# ── Parser dispatch ─────────────────────────────────────────────────────────


def _dispatch_parser(category: str, dst_path: Path, sess) -> dict:
    """Call the right parser for the category and post its journal(s).

    Returns dict: {parsed: bool, journals: int, notes: str}.
    Catches all exceptions so one bad file doesn't abort the whole pipeline.
    """
    result = {"parsed": False, "journals": 0, "notes": ""}
    try:
        if category == "cc_statement":
            from . import cc_statement_parser as ccp
            from . import cc_pipeline as ccpip
            stmt = ccp.detect_and_parse(str(dst_path))
            if not stmt:
                result["notes"] = "parse returned None"
                return result
            result["parsed"] = True
            stats = ccpip.post_statement(sess, stmt)
            sess.commit()
            result["journals"] = stats["charges"] + stats["interest"] + stats["fees"]
            result["notes"] = (f"charges={stats['charges']} int={stats['interest']} "
                               f"fees={stats['fees']} pay-skipped={stats['payments_skipped']}")
        elif category == "payslip":
            from . import payslip_parser
            p = payslip_parser.detect_and_parse(str(dst_path))
            if not p:
                result["notes"] = "parse returned None"
                return result
            result["parsed"] = True
            jid = payslip_parser.post_payslip_journal(sess, p)
            sess.commit()
            if jid:
                result["journals"] = 1
                result["notes"] = f"j={jid} gross={p.gross_pay:.2f} net={p.net_pay:.2f}"
            else:
                result["notes"] = "no journal (missing date or gross)"
        elif category == "loan_agreement":
            from . import loan_agreement_parser
            p = loan_agreement_parser.detect_and_parse(str(dst_path))
            if not p:
                result["notes"] = "parse returned None"
                return result
            result["parsed"] = True
            fid = loan_agreement_parser.upsert_facility(sess, p)
            sess.commit()
            result["notes"] = f"CreditFacility upserted: {fid}"
            # No journal posted — disbursement journal comes via Firefly bridge
        elif category == "ilp_statement":
            from . import ilp_statement_parser
            p = ilp_statement_parser.detect_and_parse(str(dst_path))
            if not p:
                result["notes"] = "parse returned None (Tokio not yet supported)"
                return result
            result["parsed"] = True
            jid = ilp_statement_parser.post_ilp_journal(sess, p)
            sess.commit()
            if jid:
                result["journals"] = 1
            result["notes"] = (f"{len(p.funds)} funds, premium={p.total_premium_this_period:.2f}, "
                               f"charges={p.total_charges_this_period:.2f}")
        elif category == "cpf_statement":
            from . import cpf_statement_parser
            p = cpf_statement_parser.detect_and_parse(str(dst_path))
            if not p:
                result["notes"] = "parse returned None"
                return result
            result["parsed"] = True
            posted = 0
            for row in p.rows:
                try:
                    jid = cpf_statement_parser.post_cpf_row_journal(sess, p, row)
                    if jid:
                        posted += 1
                except Exception as e:
                    logger.warning("CPF row post failed %s: %s", row.raw[:60], e)
                    sess.rollback()
            sess.commit()
            result["journals"] = posted
            result["notes"] = f"{len(p.rows)} rows, {posted} non-CON journals posted"
        elif category == "bank_statement":
            # POSB / Maybank / SC / Wise — Firefly bridge already covers POSB.
            # Other bank PDFs route through bank_pdf_importer if installed.
            result["notes"] = "filed (Firefly bridge handles posting)"
        elif category in ("noa_tax", "insurance_policy", "noise", "crypto_report"):
            result["notes"] = "filed (no journal for this category)"
        else:
            result["notes"] = f"no parser dispatch for category={category}"
    except Exception as e:
        try:
            sess.rollback()
        except Exception:
            pass
        result["notes"] = f"ERROR: {str(e)[:80]}"
        logger.exception("dispatch failed for %s (%s)", dst_path, category)
    return result


# ── Per-file processing ─────────────────────────────────────────────────────


def process_file(src: Path, apply: bool, post: bool, sess) -> dict:
    """Classify → route → (optionally) parse + journal."""
    r = dc.classify(src)
    final_folder = r.target_folder
    final_name = r.target_filename
    # Low-confidence override → _QUEUE (also skip parsing for unknowns)
    if r.confidence < dc.CONFIDENCE_THRESHOLD:
        final_folder = QUEUE
        final_name = src.name
    dst = final_folder / final_name
    action = "DRY-RUN"
    parse_result = {"parsed": False, "journals": 0, "notes": ""}

    if apply:
        final_folder.mkdir(parents=True, exist_ok=True)
        dst = collision_safe(dst)
        shutil.move(str(src), str(dst))
        action = "MOVED"

        if post and r.confidence >= dc.CONFIDENCE_THRESHOLD:
            parse_result = _dispatch_parser(r.category, dst, sess)
            if parse_result["parsed"]:
                action = "MOVED+POSTED" if parse_result["journals"] else "MOVED+PARSED"
            elif parse_result["notes"].startswith("ERROR"):
                action = "MOVED+ERR"

    return {
        "src": src.name,
        "category": r.category,
        "sub": r.sub_category,
        "conf": r.confidence,
        "date": r.detected_date.isoformat() if r.detected_date else "",
        "dst": str(dst.relative_to(Path("/onedrive/Sentinel Finance"))),
        "reason": r.reason,
        "action": action,
        "journals": parse_result["journals"],
        "notes": parse_result["notes"],
    }


def write_log(rows: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        ts = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            f.write(f"{ts}\t{r['action']}\t{r['category']}\t{r['sub']}\t"
                    f"{r['conf']:.2f}\t{r['src']}\t→\t{r['dst']}\t"
                    f"j={r.get('journals', 0)}\t({r.get('notes') or r['reason']})\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually move files (default: dry-run)")
    ap.add_argument("--post", action="store_true",
                    help="After moving, parse + post journals (requires --apply)")
    ap.add_argument("--watch", action="store_true",
                    help="Daemon mode: poll every 30s (use with --apply)")
    ap.add_argument("--source", default=str(INBOX),
                    help="Source folder (default: _INBOX)")
    args = ap.parse_args()

    if args.post and not args.apply:
        print("ERROR: --post requires --apply", file=sys.stderr)
        sys.exit(1)
    if args.watch and not args.apply:
        print("ERROR: --watch requires --apply", file=sys.stderr)
        sys.exit(1)

    ensure_dirs()
    src_dir = Path(args.source)
    if not src_dir.exists():
        print(f"ERROR: source folder doesn't exist: {src_dir}", file=sys.stderr)
        sys.exit(1)

    sess = None
    if args.post:
        from . import database as db
        db.init_db()
        sess = db.SessionLocal()

    def run_once() -> int:
        files = [f for f in src_dir.iterdir()
                 if f.is_file()
                 and f.suffix.lower() in (".pdf", ".csv", ".jpg", ".jpeg", ".png")]
        if not files:
            print(f"_INBOX empty ({src_dir})")
            return 0
        print(f"Scanning {len(files)} file(s) in {src_dir}")
        print(f"{'Filename':<48} {'Category':<16} {'Sub':<17} {'Conf':>5} {'Action':<13} {'J':>3}  Notes")
        print("-" * 140)
        rows = []
        for f in sorted(files):
            row = process_file(f, args.apply, args.post, sess)
            rows.append(row)
            print(f"  {row['src'][:46]:<48} {row['category']:<16} {row['sub'][:16]:<17} "
                  f"{row['conf']:>5.2f} {row['action']:<13} {row['journals']:>3}  "
                  f"{row['notes'][:60]}")
        # Summary
        cat_counts = Counter(r["category"] for r in rows)
        total_journals = sum(r["journals"] for r in rows)
        print()
        for cat, n in sorted(cat_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {cat:<22} {n:>3}")
        if args.post:
            print(f"    {'Journals posted':<22} {total_journals:>3}")
        if args.apply:
            write_log(rows)
            print(f"  Log: {LOG_PATH}")
        else:
            print()
            print("DRY-RUN — pass --apply to execute.")
        return len(rows)

    try:
        if args.watch:
            print(f"Watching {src_dir} every 30s. Ctrl-C to stop.")
            while True:
                try:
                    run_once()
                    time.sleep(30)
                except KeyboardInterrupt:
                    print("\nStopped.")
                    break
        else:
            run_once()
    finally:
        if sess:
            sess.close()


if __name__ == "__main__":
    main()
