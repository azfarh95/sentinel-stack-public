"""Fund tracker — refresh NAVs + update policy totals + write history snapshot.

Reads finance/funds.yaml as source of truth for unit holdings. For each fund,
tries the configured sources in order; on success, updates last_nav +
last_nav_date in the YAML and inserts a snapshot row into portfolio.db.

Then computes policy totals (sum of units × latest NAV) and updates the
Firefly account opening_balance for each ILP/CPF-IS account so the balance
sheet picks up the new value.

Source plugins:
  morningstar  — sg.morningstar.com lookup (server-rendered, requests-only)
  fsmone       — fundsupermart.com factsheet (JS-rendered, needs Playwright)
  manual       — uses the YAML's last_nav, flags stale if > 30 days

Run manually: py scripts/fund_tracker.py [--refresh] [--dry-run]
Or schedule via APScheduler in portfolio-mcp.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

FUNDS_PATH = Path(r"C:\Users\azfar\metamcp-local\finance\funds.yaml")
DB_PATH = Path(r"C:\Users\azfar\metamcp-local\portfolio-mcp-data\portfolio.db")  # placeholder
# Actually portfolio.db lives in container volume; for the standalone script
# we'll write to a sibling snapshots file:
HISTORY_PATH = Path(r"C:\Users\azfar\metamcp-local\finance\fund_nav_history.jsonl")

POLICY_TO_FF_ID = {
    "Tokio Marine ILP":         162,
    "Singlife Savvy Invest":    163,
    "CPF-IS":                   147,
}

STALE_DAYS = 30


def load_funds() -> dict:
    return yaml.safe_load(FUNDS_PATH.read_text())


def save_funds(data: dict):
    FUNDS_PATH.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True))


def append_history(snapshots: list[dict]):
    """One JSON-line per fund per refresh."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        for s in snapshots:
            f.write(json.dumps(s) + "\n")


# ── Source plugins ─────────────────────────────────────────────────────────────

def fetch_manual(fund: dict) -> tuple[float | None, str | None]:
    """Use the YAML's last_nav. Flag if too old."""
    if not fund.get("last_nav"):
        return None, "no manual NAV recorded"
    nav_date = fund.get("last_nav_date", "1970-01-01")
    try:
        d = date.fromisoformat(nav_date)
        age = (date.today() - d).days
    except Exception:
        age = 9999
    note = f"manual (age {age}d)"
    if age > STALE_DAYS:
        note += " STALE"
    return float(fund["last_nav"]), note


def fetch_morningstar(fund: dict) -> tuple[float | None, str | None]:
    """Stub — needs ISIN/SecID mapping. Returns None for now so we fall through to manual."""
    # TODO: implement when ISIN map is populated. Likely format:
    # https://www.morningstar.com.sg/sg/funds/snapshot/snapshot.aspx?id=<SECID>
    return None, "morningstar plugin not yet implemented"


def fetch_fsmone(fund: dict) -> tuple[float | None, str | None]:
    """Stub — fsmone is JS-rendered. Needs Playwright headless browser. Defer."""
    if not fund.get("fsmone_id"):
        return None, "no fsmone_id"
    return None, f"fsmone scraper not yet implemented (id={fund['fsmone_id']})"


SOURCE_FUNCS = {
    "manual": fetch_manual,
    "morningstar": fetch_morningstar,
    "fsmone": fetch_fsmone,
}


def refresh_one(fund: dict) -> dict:
    """Try sources in priority order. Return result dict."""
    result = {
        "fund_id": fund["id"],
        "fund_name": fund["name"],
        "nav": fund.get("last_nav"),
        "source": "stale",
        "note": "",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for src in fund.get("sources", []) or ["manual"]:
        fn = SOURCE_FUNCS.get(src)
        if not fn: continue
        nav, note = fn(fund)
        if nav is not None:
            result["nav"] = nav
            result["source"] = src
            result["note"] = note or ""
            return result
        result["note"] = note or ""
    return result


# ── Policy totals + Firefly sync ───────────────────────────────────────────────

def compute_policy_totals(data: dict, fx_to_sgd: float) -> dict:
    """Per-policy sum of units × NAV. USD funds converted to SGD via fx_to_sgd."""
    totals = {}
    for fund in data["funds"]:
        nav = float(fund.get("last_nav") or 0)
        ccy = fund.get("currency", "SGD")
        for h in fund.get("holdings", []) or []:
            policy = h["policy"]
            units = float(h["units"])
            sgd = units * nav * (fx_to_sgd if ccy == "USD" else 1.0)
            totals[policy] = totals.get(policy, 0.0) + sgd
    today = date.today().isoformat()
    return {p: {"sgd": round(v, 2), "last_calc": today} for p, v in totals.items()}


def push_to_firefly(policy_totals: dict, dry_run: bool = False) -> dict:
    pat = os.environ.get("FIREFLY_PAT") or ""
    if not pat:
        pat_file = Path(os.environ.get("TEMP", "")) / "firefly_pat.txt"
        if pat_file.exists():
            pat = pat_file.read_text().strip()
    if not pat:
        return {"error": "no FIREFLY_PAT"}

    results = {}
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json", "Content-Type": "application/json"}
    with httpx.Client(timeout=15) as c:
        for policy, info in policy_totals.items():
            ff_id = POLICY_TO_FF_ID.get(policy)
            if not ff_id:
                results[policy] = "no firefly mapping"
                continue
            target_sgd = info["sgd"]
            # Need to compute new opening_balance so current_balance == target
            r = c.get(f"http://127.0.0.1:8180/api/v1/accounts/{ff_id}", headers=headers)
            attrs = r.json()["data"]["attributes"]
            current = float(attrs["current_balance"])
            opening = float(attrs.get("opening_balance") or 0)
            net = current - opening
            new_opening = round(target_sgd - net, 2)
            if dry_run:
                results[policy] = f"DRY: would set opening={new_opening} (target {target_sgd}, current {current})"
                continue
            r = c.put(f"http://127.0.0.1:8180/api/v1/accounts/{ff_id}", headers=headers,
                      json={"opening_balance": str(new_opening),
                            "opening_balance_date": attrs.get("opening_balance_date", "2026-01-01")[:10]})
            if r.status_code == 200:
                results[policy] = f"updated to SGD {target_sgd:,.2f}"
            else:
                results[policy] = f"FAIL [{r.status_code}]: {r.text[:80]}"
    return results


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="Attempt to fetch fresh NAVs from configured sources (default: use manual only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute everything but don't write Firefly or YAML")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = load_funds()
    cfg = yaml.safe_load(Path(r"C:\Users\azfar\metamcp-local\finance\balance_sheet_config.yaml").read_text())
    fx_to_sgd = float(cfg.get("usd_to_sgd", 1.27))

    snapshots = []
    stale_funds = []
    for fund in data["funds"]:
        r = refresh_one(fund)
        snapshots.append(r)
        if "STALE" in r.get("note", ""):
            stale_funds.append(fund["name"])
        if args.refresh and r["source"] != "stale" and r["source"] != "manual":
            # Persist fresh NAV back into YAML
            fund["last_nav"] = r["nav"]
            fund["last_nav_date"] = date.today().isoformat()
        if not args.quiet:
            logger.info("%-50s NAV %10.4f  %s", fund["name"][:50], r["nav"] or 0, r.get("note", ""))

    if not args.dry_run:
        append_history(snapshots)
        if args.refresh:
            save_funds(data)

    # Policy totals
    totals = compute_policy_totals(data, fx_to_sgd)
    logger.info("=== Policy totals (FX USD->SGD %.4f) ===", fx_to_sgd)
    for policy, info in totals.items():
        logger.info("  %-26s SGD %10.2f  as of %s", policy, info["sgd"], info["last_calc"])

    # Push to Firefly (unless dry-run)
    if not args.dry_run:
        push_results = push_to_firefly(totals, dry_run=False)
        logger.info("=== Firefly sync ===")
        for k, v in push_results.items():
            logger.info("  %-26s %s", k, v)

    if stale_funds:
        logger.warning("Stale NAVs (>%dd old): %s", STALE_DAYS, ", ".join(stale_funds))

    return 0


if __name__ == "__main__":
    sys.exit(main())
