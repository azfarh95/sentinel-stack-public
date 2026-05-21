"""Morningstar SG NAV scraper.

For each fund in /finance/funds.yaml that lists "morningstar" in its
`sources` list, fetch the latest NAV + date from Morningstar Singapore.

Endpoints:
  Search (returns Morningstar SecID):
    https://www.morningstar.com.sg/sg/util/SecuritySearch.ashx?source=nav&q={QUERY}
  Snapshot (server-rendered HTML, contains current NAV):
    https://www.morningstar.com.sg/sg/funds/snapshot/snapshot.aspx?id={SECID}

To skip the search step (faster + more reliable), each fund can list its
Morningstar SecID directly in funds.yaml as `morningstar_id`. If absent, we
fall back to a fuzzy search by `name`.

Updates funds.yaml in place: `last_nav` + `last_nav_date`. Idempotent —
re-running the same day produces the same values.

Scheduled daily 06:00 via APScheduler. Manual trigger via `morningstar_refresh()`
MCP tool.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

FUNDS_PATH = Path("/finance/funds.yaml")
SEARCH_URL = "https://www.morningstar.com.sg/sg/util/SecuritySearch.ashx"
SNAPSHOT_URL = "https://www.morningstar.com.sg/sg/funds/snapshot/snapshot.aspx"
USER_AGENT = "Mozilla/5.0 (compatible; SentinelFinance/1.8)"

# Regex hunters for the Morningstar SG snapshot page. Layout has been stable
# since 2024 — NAV sits in a definition list near the top with class "snapshotTitleTable".
NAV_RE = re.compile(r"(?i)(?:NAV|Latest Price|Last Price)[^\d]{0,30}([\d,.]+)")
DATE_RE = re.compile(r"(\d{1,2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(\d{2,4})")
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


async def search_secid(query: str) -> str | None:
    """Best-match Morningstar SecID for a fund name. None if no result."""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(SEARCH_URL, params={"source": "nav", "q": query})
        if r.status_code != 200 or not r.text.strip():
            return None
        # Endpoint returns either JSON or pipe-delimited; try both
        try:
            data = r.json()
            if isinstance(data, list) and data:
                # Each row: {"SecId": "F0...", "Name": "..."}
                return data[0].get("SecId") or data[0].get("secId")
        except Exception:
            pass
        # Pipe-delimited fallback: "Name|SecId|Type|..."
        first_line = r.text.splitlines()[0] if r.text else ""
        parts = first_line.split("|")
        if len(parts) > 1 and parts[1]:
            return parts[1]
        return None
    except Exception:
        logger.exception("morningstar search failed for %r", query)
        return None


async def fetch_nav(secid: str) -> tuple[float | None, str | None]:
    """Hit the snapshot page and parse NAV + as-of date.
    Returns (nav, iso_date) where either may be None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(SNAPSHOT_URL, params={"id": secid})
        if r.status_code != 200:
            return None, None
        soup = BeautifulSoup(r.text, "html.parser")
        # Strategy 1: definition list / table rows containing "NAV"
        nav = None
        nav_date = None
        text = soup.get_text(" ", strip=True)
        m = NAV_RE.search(text)
        if m:
            try:
                nav = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        # Find a date near the NAV — Morningstar SG typically prints "As at DD/MM/YYYY"
        as_at = re.search(r"As\s*at\s*([\d/\-\.]+)", text, re.IGNORECASE)
        candidate = as_at.group(1) if as_at else None
        if candidate:
            d = ISO_DATE_RE.search(candidate)
            if d:
                y, mo, day = d.groups()
                nav_date = f"{int(y):04d}-{int(mo):02d}-{int(day):02d}"
            else:
                d2 = DATE_RE.search(candidate)
                if d2:
                    day, mo, y = d2.groups()
                    y = int(y) + 2000 if len(y) == 2 else int(y)
                    try:
                        nav_date = date(y, int(mo), int(day)).isoformat()
                    except ValueError:
                        pass
        return nav, nav_date
    except Exception:
        logger.exception("morningstar snapshot failed for %s", secid)
        return None, None


def _write_nav_history(funds: list, source: str = "morningstar") -> int:
    """Append/update nav_history rows from funds.yaml entries."""
    from datetime import datetime
    from sqlalchemy import select
    from . import database as db
    from . import ledger
    s = db.SessionLocal()
    written = 0
    try:
        now = db.now_utc()
        for f in funds:
            if not f.get("last_nav") or not f.get("last_nav_date"):
                continue
            try:
                nav_d = datetime.strptime(f["last_nav_date"], "%Y-%m-%d").date()
            except Exception:
                continue
            existing = s.execute(
                select(ledger.NavHistory).where(
                    ledger.NavHistory.fund_id == f["id"],
                    ledger.NavHistory.nav_date == nav_d,
                )
            ).scalar_one_or_none()
            if existing:
                existing.nav_price = float(f["last_nav"])
                existing.source = source
                existing.fund_name = f.get("name", "")
            else:
                s.add(ledger.NavHistory(
                    fund_id=f["id"], fund_name=f.get("name", ""),
                    nav_date=nav_d, nav_price=float(f["last_nav"]),
                    currency=f.get("currency", "SGD"), source=source,
                    created_at=now,
                ))
                written += 1
        s.commit()
    finally:
        s.close()
    return written


async def refresh_all(dry_run: bool = False) -> dict:
    """Walk funds.yaml; for each Morningstar-sourced fund, refresh NAV.

    Returns: {scanned, updated, skipped, errors: list[str], details: [per-fund]}.
    """
    try:
        data = yaml.safe_load(FUNDS_PATH.read_text())
    except FileNotFoundError:
        return {"scanned": 0, "updated": 0, "skipped": 0, "errors": ["funds.yaml not found"]}

    scanned = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    details: list[dict] = []

    funds = data.get("funds", [])
    for f in funds:
        sources = f.get("sources") or []
        if "morningstar" not in sources:
            continue
        scanned += 1
        name = f.get("name", f.get("id", "?"))
        secid = f.get("morningstar_id")
        if not secid:
            secid = await search_secid(name)
            if not secid:
                skipped += 1
                details.append({"fund": name, "status": "no SecId found"})
                continue
            f["morningstar_id"] = secid  # cache for next run
        nav, nav_date = await fetch_nav(secid)
        if nav is None:
            skipped += 1
            errors.append(f"{name}: NAV not parseable from snapshot")
            details.append({"fund": name, "secid": secid, "status": "parse failed"})
            continue
        old_nav = f.get("last_nav")
        f["last_nav"] = round(nav, 4)
        f["last_nav_date"] = nav_date or date.today().isoformat()
        updated += 1
        details.append({
            "fund": name, "secid": secid,
            "old_nav": old_nav, "new_nav": f["last_nav"],
            "nav_date": f["last_nav_date"],
            "status": "updated",
        })

    if updated and not dry_run:
        FUNDS_PATH.write_text(yaml.safe_dump(data, sort_keys=False,
                                              default_flow_style=False))
        logger.info("morningstar: %d funds updated in funds.yaml", updated)
        # Also write each NAV to nav_history table (time-series persistence)
        try:
            _write_nav_history(funds, source="morningstar")
        except Exception:
            logger.exception("nav_history write failed (non-fatal)")

    return {
        "scanned": scanned,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "details": details,
        "ran_at": datetime.utcnow().isoformat() + "Z",
        "dry_run": dry_run,
    }
