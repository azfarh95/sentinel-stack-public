"""
portfolio-mcp  —  Multi-chain Web3 wallet snapshots via Moralis.

MCP Tools:
  portfolio_snapshot   Take a fresh snapshot of a wallet across all chains.
  portfolio_latest     Return the most recent stored snapshot.
  portfolio_history    Return a time series of total USD value over the past N days.
  portfolio_diff       Compare two snapshots (last vs N days ago).
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse, FileResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from decimal import Decimal

from . import agent_api
from . import auth
from . import balance_sheet as bs
from . import coa_view
from . import config
from . import credit_utilization
from . import bot as tg_bot
from . import database as db
from . import dexscreener
from . import cash_forecast as cf_mod
from . import drill as drill_mod
from . import home as home_mod
from . import income_statement as is_mod
from . import jobs as jobs_mod
from . import wise as wise_mod
from . import moralis
from . import polling
from . import v2_dashboards
from . import wolfswap

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Re-export config constants for back-compat with the rest of main.py +
# any other module that has imported these names from `app.main`.
DEFAULT_ADDR = config.DEFAULT_ADDR
DUST = config.DUST
POLL_INTERVAL_MIN = config.POLL_INTERVAL_MIN

scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def _lifespan(server: FastMCP):
    """Container lifecycle: init DB, register jobs (jobs.py owns wiring),
    run startup catch-ups, start scheduler + Telegram bot listener."""
    db.init_db()
    logger.info("portfolio-mcp ready (dust threshold=$%s)", DUST)

    if DEFAULT_ADDR and not scheduler.running:
        # Bookmark current head so historical txs don't trigger alerts on first poll
        try:
            await polling.initialize_bookmarks(DEFAULT_ADDR)
        except Exception:
            logger.exception("bookmark init failed; continuing")

        # All scheduler wiring lives in jobs.py
        registered = jobs_mod.register_jobs(scheduler)
        logger.info("scheduler jobs registered: %s", ", ".join(registered))

        # Startup catch-ups (intentional one-shot kicks; failures non-fatal)
        if config.WISE_API_TOKEN:
            try:
                await jobs_mod.wise_sync()
                logger.info("Wise sync startup catch-up ran")
            except Exception:
                logger.exception("Wise startup catch-up failed; continuing")

        try:
            await jobs_mod.networth_snapshot()
        except Exception:
            logger.exception("initial NW snapshot failed; continuing")

        try:
            await jobs_mod.firefly_bridge()
            logger.info("Firefly bridge startup catch-up ran")
        except Exception:
            logger.exception("Firefly bridge startup catch-up failed; continuing")

        try:
            await jobs_mod.refresh_manual_prices()
        except Exception:
            logger.exception("initial price refresh failed; continuing")

        scheduler.start()
        logger.info("schedulers started (onchain poll %dm, price refresh 60m)",
                    POLL_INTERVAL_MIN)

        # Bot listener disabled by default — see BOT_LISTENER_ENABLED rationale
        # in config.py / earlier comments.
        if config.BOT_LISTENER_ENABLED:
            try:
                await tg_bot.start_bot()
            except Exception:
                logger.exception("telegram bot failed to start; continuing without listener")
        else:
            logger.info("bot listener disabled (BOT_LISTENER_ENABLED!=1)")

    yield
    await tg_bot.stop_bot()
    if scheduler.running:
        scheduler.shutdown(wait=False)


mcp = FastMCP(
    "Portfolio",
    lifespan=_lifespan,
    instructions=(
        "Multi-chain Web3 wallet tracker. Takes a public EVM address, returns USD holdings "
        "across Ethereum, BSC, Cronos, Polygon, Arbitrum, Base, Avalanche, zkSync. "
        "Default wallet is configured via PORTFOLIO_DEFAULT_ADDRESS env var — if unset, the "
        "user must supply an address argument. Snapshots are stored in SQLite for history. "
        "Dust threshold: positions under $%.2f are filtered from snapshots." % DUST
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*", "localhost:*", "[::1]:*",
            "host.docker.internal:*", "portfolio-mcp:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            "http://host.docker.internal:*", "http://portfolio-mcp:*",
        ],
    ),
)


def _resolve_addr(address: str | None) -> str:
    addr = (address or DEFAULT_ADDR).strip().lower()
    if not addr:
        raise ValueError("No address provided and PORTFOLIO_DEFAULT_ADDRESS is unset")
    if not addr.startswith("0x") or len(addr) != 42:
        raise ValueError(f"Invalid EVM address: {addr}")
    return addr


def _hidden_set(s) -> set:
    """Return {(chain, token_address_lower)} for currently hidden tokens."""
    rows = s.query(db.HiddenToken).all()
    return {(r.chain, (r.token_address or "").lower()) for r in rows}


@mcp.tool()
async def portfolio_snapshot(address: str | None = None, save: bool = True) -> dict:
    """Fetch a fresh snapshot across all chains. Hidden tokens are excluded."""
    addr = _resolve_addr(address)
    # Dust threshold can be edited live via /config/datetime; falls back to
    # the PORTFOLIO_DUST_USD env var if settings.yaml is missing the key.
    from . import settings as _settings
    snap = await moralis.wallet_snapshot(addr, dust_threshold_usd=_settings.dust_usd())
    # Strip hidden tokens
    s = db.SessionLocal()
    try:
        hidden = _hidden_set(s)
    finally:
        s.close()
    if hidden:
        snap["positions"] = [
            p for p in snap["positions"]
            if (p["chain"], (p["token_address"] or "").lower()) not in hidden
        ]
    snap["liquid_usd"] = round(sum(p["usd_value"] for p in snap["positions"]), 2)
    snap["token_count"] = len(snap["positions"])

    # Add manual DeFi positions
    s = db.SessionLocal()
    try:
        manual_rows = s.query(db.ManualPosition).all()
    finally:
        s.close()
    manual_positions = [
        {"label": r.label, "chain": r.chain, "protocol": r.protocol,
         "usd_value": r.usd_value, "source": "manual"}
        for r in manual_rows
    ]

    # Krystal LP/vault positions (invisible to Moralis /tokens). Cached 15min.
    try:
        from . import krystal as _krystal
        kr = await _krystal.get_positions(addr)
        krystal_summary = _krystal.summarise_for_snapshot(kr.get("positions", []))
        snap["krystal_cached"] = kr.get("cached")
        snap["krystal_age_s"] = kr.get("age_s")
    except Exception as e:
        logger.exception("krystal fetch failed")
        krystal_summary = []
        snap["krystal_error"] = str(e)[:120]

    snap["manual_positions"] = manual_positions + krystal_summary
    snap["manual_usd"] = round(sum(p["usd_value"] for p in snap["manual_positions"]), 2)
    snap["total_usd"] = round(snap["liquid_usd"] + snap["manual_usd"], 2)

    # ── Pre-rendered display block — OpenClaw renders this verbatim ─────────
    from datetime import datetime, timezone as _tz
    top5_liquid = sorted(snap["positions"], key=lambda p: -p["usd_value"])[:5]
    top3_manual = sorted(snap["manual_positions"], key=lambda p: -p["usd_value"])[:3]
    short_addr = f"{snap['address'][:6]}...{snap['address'][-4:]}"

    def liquid_row(i, p):
        return f"| {i} | {p['symbol']:<8} | {p['chain']:<8} | ${p['usd_value']:>9,.2f} |"
    def manual_row(i, m):
        proto = m.get("protocol", "?")[:10]
        label = m["label"][:24]
        return f"| {i} | {label:<24} | {proto:<10} | ${m['usd_value']:>9,.2f} |"

    lines = [
        f"💼 *Wallet Snapshot — {datetime.now(_tz.utc).strftime('%d %b %Y %H:%M UTC')}*",
        f"Address: `{short_addr}`",
        f"",
        f"*Total: ${snap['total_usd']:,.2f}*",
        f"  Liquid:  ${snap['liquid_usd']:,.2f}",
        f"  Manual:  ${snap['manual_usd']:,.2f}",
        f"",
        f"*Top 5 token holdings*",
        f"```",
        f"| # | Token    | Chain    | USD       |",
        f"|---|----------|----------|-----------|",
    ]
    for i, p in enumerate(top5_liquid, 1):
        lines.append(liquid_row(i, p))
    lines.append("```")
    lines.append("")
    lines.append("*Top 3 staking / LP positions*")
    if top3_manual:
        lines.append("```")
        lines.append("| # | Position                 | Protocol   | USD       |")
        lines.append("|---|--------------------------|------------|-----------|")
        for i, m in enumerate(top3_manual, 1):
            lines.append(manual_row(i, m))
        lines.append("```")
    else:
        lines.append("_(none recorded)_")
    lines.append("")
    lines.append("_via Portfolio MCP_")
    snap["display_text"] = "\n".join(lines)
    if save:
        s = db.SessionLocal()
        try:
            row = db.Snapshot(
                address=addr,
                captured_at=db.now_utc(),
                total_usd=snap["total_usd"],
                chain_count=snap["chain_count"],
                token_count=snap["token_count"],
            )
            s.add(row)
            s.flush()
            for p in snap["positions"]:
                s.add(db.Position(
                    snapshot_id=row.id,
                    chain=p["chain"],
                    token_address=p["token_address"],
                    symbol=p["symbol"],
                    decimals=p["decimals"],
                    raw_balance=p["raw_balance"],
                    usd_price=p["usd_price"],
                    usd_value=p["usd_value"],
                ))
            s.commit()
            snap["snapshot_id"] = row.id
        finally:
            s.close()
    return snap


@mcp.tool()
async def portfolio_latest(address: str | None = None) -> dict:
    """Return the most recent stored snapshot, with top 20 positions by USD."""
    addr = _resolve_addr(address)
    s = db.SessionLocal()
    try:
        snap = (s.query(db.Snapshot)
                  .filter(db.Snapshot.address == addr)
                  .order_by(db.Snapshot.captured_at.desc())
                  .first())
        if not snap:
            return {"error": "no snapshots stored — call portfolio_snapshot first"}
        positions = (s.query(db.Position)
                       .filter(db.Position.snapshot_id == snap.id)
                       .order_by(db.Position.usd_value.desc())
                       .limit(20).all())
        return {
            "snapshot_id": snap.id,
            "address": snap.address,
            "captured_at": snap.captured_at.isoformat(),
            "total_usd": snap.total_usd,
            "chain_count": snap.chain_count,
            "token_count": snap.token_count,
            "top_positions": [
                {"chain": p.chain, "symbol": p.symbol, "usd_value": p.usd_value}
                for p in positions
            ],
        }
    finally:
        s.close()


@mcp.tool()
async def portfolio_history(address: str | None = None, days: int = 30) -> dict:
    """Time series of total USD across the last N days."""
    addr = _resolve_addr(address)
    cutoff = db.now_utc() - timedelta(days=days)
    s = db.SessionLocal()
    try:
        snaps = (s.query(db.Snapshot)
                   .filter(db.Snapshot.address == addr,
                           db.Snapshot.captured_at >= cutoff)
                   .order_by(db.Snapshot.captured_at.asc()).all())
        return {
            "address": addr,
            "days": days,
            "series": [
                {"at": s.captured_at.isoformat(), "total_usd": s.total_usd}
                for s in snaps
            ],
        }
    finally:
        s.close()


@mcp.tool()
async def portfolio_diff(address: str | None = None, days: int = 1) -> dict:
    """Compare latest snapshot to one taken ~N days ago. Reports net USD delta + per-chain swing."""
    addr = _resolve_addr(address)
    cutoff = db.now_utc() - timedelta(days=days)
    s = db.SessionLocal()
    try:
        latest = (s.query(db.Snapshot)
                    .filter(db.Snapshot.address == addr)
                    .order_by(db.Snapshot.captured_at.desc()).first())
        previous = (s.query(db.Snapshot)
                      .filter(db.Snapshot.address == addr,
                              db.Snapshot.captured_at <= cutoff)
                      .order_by(db.Snapshot.captured_at.desc()).first())
        if not latest or not previous:
            return {"error": f"need at least one snapshot before and after {days}d ago"}

        def by_chain(snap_id: int) -> dict:
            rows = s.query(db.Position).filter(db.Position.snapshot_id == snap_id).all()
            agg = {}
            for r in rows:
                agg[r.chain] = agg.get(r.chain, 0.0) + r.usd_value
            return agg

        l_chains = by_chain(latest.id)
        p_chains = by_chain(previous.id)
        keys = sorted(set(l_chains) | set(p_chains))
        chain_diff = [
            {"chain": k,
             "now_usd": round(l_chains.get(k, 0.0), 2),
             "then_usd": round(p_chains.get(k, 0.0), 2),
             "delta_usd": round(l_chains.get(k, 0.0) - p_chains.get(k, 0.0), 2)}
            for k in keys
        ]
        return {
            "address": addr,
            "now": {"at": latest.captured_at.isoformat(), "total_usd": latest.total_usd},
            "then": {"at": previous.captured_at.isoformat(), "total_usd": previous.total_usd},
            "delta_usd": round(latest.total_usd - previous.total_usd, 2),
            "by_chain": chain_diff,
        }
    finally:
        s.close()


@mcp.tool()
async def cashflow_text() -> str:
    """Reads finance/liabilities-registry.yaml and returns upcoming debits ordered
    by date. Shows next 7 days, next 30 days, then totals. Render verbatim for
    the /cashflow command. Do not summarise or reformat.
    """
    import yaml
    from datetime import datetime, date, timedelta, timezone as _tz
    try:
        with open("/finance/liabilities-registry.yaml") as f:
            reg = yaml.safe_load(f)
    except FileNotFoundError:
        return "Registry not found at /finance/liabilities-registry.yaml."

    today = date.today()

    def next_billing(day: int) -> date:
        """Return next future date with this day-of-month."""
        day = min(day, 28)  # clamp for short months
        if today.day < day:
            return date(today.year, today.month, day)
        y, m = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        return date(y, m, day)

    # Build (next_date, account, amount) tuples for each account's monthly debit
    debits = []
    total_outstanding = 0.0
    for acct in reg.get("accounts", []):
        amount = sum(p["monthly"] for p in acct["plans"])
        bday = acct["billing_day"]
        nd = next_billing(bday)
        short = acct["name"].split("(")[0].strip()[:24]
        debits.append((nd, short, amount, acct["current_outstanding"]))
        total_outstanding += acct["current_outstanding"]
    debits.sort()

    total_monthly = sum(d[2] for d in debits)
    week_end = today + timedelta(days=7)
    next_30 = today + timedelta(days=30)

    def fmt_row(d, name, amt):
        days_away = (d - today).days
        suffix = f"in {days_away}d" if days_away > 0 else ("TODAY" if days_away == 0 else f"{-days_away}d ago")
        return f"  {d.strftime('%d %b'):<7} {name:<26} ${amt:>8,.2f}  ({suffix})"

    week_debits = [d for d in debits if today <= d[0] <= week_end]
    month_debits = [d for d in debits if today <= d[0] <= next_30]

    def row(d, name, amt):
        days = (d - today).days
        when = "TODAY" if days == 0 else (f"+{days}d" if days > 0 else f"{days}d")
        return f"| {d.strftime('%d %b'):<6} | {name[:22]:<22} | ${amt:>8,.2f} | {when:>5} |"

    lines = [
        f"📊 *Upcoming Debits — {datetime.now(_tz.utc).strftime('%d %b %Y %H:%M UTC')}*",
        "",
    ]
    if week_debits:
        lines.append("*⚡ Next 7 days*")
        lines.append("```")
        lines.append("| Date   | Account                | Amount    | When  |")
        lines.append("|--------|------------------------|-----------|-------|")
        for d, name, amt, _ in week_debits:
            lines.append(row(d, name, amt))
        sum_week = sum(d[2] for d in week_debits)
        lines.append(f"|        | 7-day total            | ${sum_week:>8,.2f} |       |")
        lines.append("```")
        lines.append("")

    lines.append("*📅 All upcoming (next ~30 days)*")
    lines.append("```")
    lines.append("| Date   | Account                | Amount    | When  |")
    lines.append("|--------|------------------------|-----------|-------|")
    for d, name, amt, _ in month_debits:
        lines.append(row(d, name, amt))
    sum_month = sum(d[2] for d in month_debits)
    lines.append(f"|        | 30-day total           | ${sum_month:>8,.2f} |       |")
    lines.append("```")
    lines.append("")
    lines.append(f"Monthly obligation: *${total_monthly:,.2f}*")
    lines.append(f"Total outstanding:  *${total_outstanding:,.2f}*")
    lines.append("")
    lines.append("_via Portfolio MCP_")
    return "\n".join(lines)


@mcp.tool()
async def balance_text() -> str:
    """Queries Firefly III for asset + liability totals. Returns PRE-FORMATTED
    STRING. Render verbatim for the /balance command. Do not summarise."""
    from datetime import datetime, timezone as _tz
    import httpx, os
    pat_path = "/firefly_pat.txt"  # not mounted; fall back to env
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat and os.path.exists(pat_path):
        pat = open(pat_path).read().strip()
    if not pat:
        return "Firefly PAT not configured (set FIREFLY_PAT env var or mount /firefly_pat.txt)."

    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15) as c:
        assets = (await c.get("http://host.docker.internal:8180/api/v1/accounts?type=asset",
                              headers=headers)).json().get("data", [])
        liabs = (await c.get("http://host.docker.internal:8180/api/v1/accounts?type=liabilities",
                             headers=headers)).json().get("data", [])

    asset_sgd = 0.0
    asset_usd = 0.0
    for a in assets:
        bal = float(a["attributes"]["current_balance"])
        cur = a["attributes"]["currency_code"]
        if cur == "SGD": asset_sgd += bal
        elif cur == "USD": asset_usd += bal
    liab_sgd = sum(float(a["attributes"]["current_balance"]) for a in liabs)
    asset_sgd_total = asset_sgd + asset_usd * 1.34  # rough USD→SGD
    net_worth = asset_sgd_total + liab_sgd

    lines = [
        f"💰 *Balance Sheet — {datetime.now(_tz.utc).strftime('%d %b %Y %H:%M UTC')}*",
        f"",
        f"```",
        f"| Item            | SGD          |",
        f"|-----------------|--------------|",
        f"| SGD assets      | {asset_sgd:>11,.2f}  |",
        f"| USD assets      | {asset_usd*1.34:>11,.2f}  |",
        f"| Total assets    | {asset_sgd_total:>11,.2f}  |",
        f"| Liabilities     | {liab_sgd:>11,.2f}  |",
        f"|-----------------|--------------|",
        f"| NET WORTH       | {net_worth:>11,.2f}  |",
        f"```",
        f"",
        f"_via Portfolio MCP_",
    ]
    return "\n".join(lines)


@mcp.tool()
async def wallet_snapshot_text() -> str:
    """RETURNS THE WALLET SNAPSHOT AS A PRE-FORMATTED STRING. Render this output
    VERBATIM in chat — do not reformat, do not summarise, do not re-extract fields.
    The output is already the exact human-readable layout the user wants.

    This is the tool to call for the /wallet_snapshot command. Always use this
    instead of portfolio_snapshot when the user wants a chat-ready view.
    """
    snap = await portfolio_snapshot(address=None, save=False)
    return snap.get("display_text", "(no snapshot available)")


@mcp.tool()
async def wise_sync() -> dict:
    """Force an immediate Wise balance + transaction sync to Firefly. Useful for testing
    or after rotating the Wise API token. Returns per-currency balances + final SGD total."""
    return await wise_mod.sync_now()


@mcp.tool()
async def onchain_poll_now() -> dict:
    """Force an immediate onchain transaction poll. Emits Telegram alerts for any new
    transactions where USD value exceeds the threshold. Useful for testing."""
    n = await polling.poll_once(DEFAULT_ADDR)
    return {"ok": True, "alerts_sent": n}


@mcp.tool()
async def reconcile_now(days: int = 60) -> dict:
    """Match POSB outflows against CC charges over the trailing N days.
    Returns {matched, unmatched_posb, unmatched_cc, totals, window}."""
    from . import reconcile as _rec
    return await _rec.run_reconcile(days=days)


@mcp.tool()
async def import_bank_pdf(path: str) -> dict:
    """Parse a Maybank or Standard Chartered savings PDF statement and
    POST each row to Firefly. Returns import summary with variance vs
    statement closing balance."""
    from . import bank_pdf_importer as _bpi
    from pathlib import Path as _Path
    return _bpi.import_pdf(_Path(path))


@mcp.tool()
async def classifier_reload() -> dict:
    """Force a reload of finance/classifier.yaml. Call after editing the YAML.
    Returns vendor count + stats."""
    from . import classifier as _cls
    n = _cls.reload_classifier()
    return {"reloaded": True, "vendor_count": n, "stats": _cls.stats()}


@mcp.tool()
async def portfolio_chart(period_days: int = 30, send: bool = False,
                          channel: str = "testbot") -> dict:
    """Render net-worth history over the trailing N days as a PNG chart,
    saved to /data/charts/. If send=True, also delivers via Telegram
    (channel='testbot' for dev, 'production' for owner-facing alerts)."""
    from . import portfolio_chart as _chart
    r = _chart.build_png(period_days=period_days)
    if send and r.get("path"):
        caption = (f"Net Worth: SGD {r['last_nw_sgd']:,.2f}\n"
                   f"Change {period_days}d: SGD {r['net_change_sgd']:+,.2f}\n"
                   f"{r['start']} → {r['end']} ({r['n_points']} points)")
        r["telegram"] = await _chart.send_to_telegram(r["path"], caption, channel=channel)
    return r


@mcp.tool()
async def morningstar_refresh() -> dict:
    """Force an immediate Morningstar SG NAV refresh for every fund in
    funds.yaml that lists 'morningstar' in its sources. Updates funds.yaml
    in place. Returns scanned/updated/skipped counts + per-fund details."""
    from . import morningstar_sg as _ms
    return await _ms.refresh_all()


@mcp.tool()
async def backup_now() -> dict:
    """Force an immediate Sentinel Finance backup (finance YAMLs + Firefly REST export).
    Writes tar.gz to /data/backups/sentinel-finance-YYYY-MM-DD.tar.gz. Returns manifest."""
    from . import backup as _backup
    return await _backup.run_backup()


@mcp.tool()
async def backup_list() -> dict:
    """List existing Sentinel Finance backups in /data/backups, newest first."""
    from . import backup as _backup
    return {"backups": _backup.list_backups()}


@mcp.tool()
async def onchain_reset_bookmarks() -> dict:
    """Wipe all last-seen-tx bookmarks. The NEXT poll will re-baseline silently
    (no historical alerts). Use after changing the alert filter or to fix loops."""
    s = db.SessionLocal()
    try:
        n = s.query(db.LastSeenTx).delete()
        s.commit()
        return {"ok": True, "bookmarks_cleared": n}
    finally:
        s.close()


@mcp.tool()
async def portfolio_set_manual_auto(label: str, chain: str, protocol: str,
                                     token_chain: str, token_address: str,
                                     token_amount: str, token_symbol: str = "",
                                     notes: str = "") -> dict:
    """Record a DeFi position with AUTO-PRICED USD. USD value is recomputed daily
    from DexScreener. Use this for staking/LP positions where the token amount
    rarely changes but price moves daily.

    Args:
      label:         unique name e.g. "WolfSwap PACK stake"
      chain:         display chain for the protocol
      protocol:      e.g. "WolfSwap"
      token_chain:   DexScreener chain slug (eth/bsc/polygon/arbitrum/base/avalanche/cronos)
      token_address: ERC20 contract for the staked token
      token_amount:  decimal string e.g. "52500000.0" — only changes on (un)stake
      token_symbol:  display label e.g. "PACK"

    Fetches a current price immediately to validate + populate usd_value.
    """
    # Validate by fetching a price now
    price = await dexscreener.token_price(token_chain, token_address)
    amount = Decimal(token_amount)
    usd_value = float(amount * Decimal(str(price["price_usd"])))

    s = db.SessionLocal()
    try:
        row = s.query(db.ManualPosition).filter(db.ManualPosition.label == label).first()
        if row:
            row.chain = chain
            row.protocol = protocol
            row.token_chain = token_chain
            row.token_address = token_address.lower()
            row.token_amount = token_amount
            row.token_symbol = token_symbol or price.get("symbol")
            row.last_price_usd = price["price_usd"]
            row.last_priced_at = db.now_utc()
            row.usd_value = usd_value
            row.notes = notes or row.notes
            row.updated_at = db.now_utc()
            action = "updated"
        else:
            row = db.ManualPosition(
                label=label, chain=chain, protocol=protocol,
                token_chain=token_chain, token_address=token_address.lower(),
                token_amount=token_amount,
                token_symbol=token_symbol or price.get("symbol"),
                last_price_usd=price["price_usd"], last_priced_at=db.now_utc(),
                usd_value=usd_value, notes=notes or None,
                updated_at=db.now_utc(),
            )
            s.add(row)
            action = "added"
        s.commit()
        return {"ok": True, "action": action, "id": row.id, "label": label,
                "price_usd": price["price_usd"], "usd_value": usd_value,
                "liquidity_usd": price["liquidity_usd"], "dex": price["dex"]}
    finally:
        s.close()


@mcp.tool()
async def portfolio_set_manual(label: str, chain: str, usd_value: float,
                                protocol: str = "", notes: str = "") -> dict:
    """Record/update a DeFi position Moralis can't see (staking, LP, vaults). Idempotent by label.
    Example: portfolio_set_manual(label='WolfSwap PACK stake', chain='cronos',
                                   usd_value=7792.00, protocol='WolfSwap')"""
    s = db.SessionLocal()
    try:
        row = s.query(db.ManualPosition).filter(db.ManualPosition.label == label).first()
        if row:
            row.chain = chain
            row.protocol = protocol or row.protocol
            row.usd_value = float(usd_value)
            row.notes = notes or row.notes
            row.updated_at = db.now_utc()
            action = "updated"
        else:
            row = db.ManualPosition(
                label=label, chain=chain, protocol=protocol or None,
                usd_value=float(usd_value), notes=notes or None,
                updated_at=db.now_utc(),
            )
            s.add(row)
            action = "added"
        s.commit()
        return {"ok": True, "action": action, "id": row.id, "label": label, "usd_value": row.usd_value}
    finally:
        s.close()


@mcp.tool()
async def portfolio_list_manual() -> dict:
    """List all manually-tracked DeFi positions."""
    s = db.SessionLocal()
    try:
        rows = s.query(db.ManualPosition).order_by(db.ManualPosition.usd_value.desc()).all()
        total = sum(r.usd_value for r in rows)
        return {
            "count": len(rows),
            "total_usd": round(total, 2),
            "positions": [
                {"id": r.id, "label": r.label, "chain": r.chain, "protocol": r.protocol,
                 "usd_value": r.usd_value, "notes": r.notes,
                 "updated_at": r.updated_at.isoformat()}
                for r in rows
            ],
        }
    finally:
        s.close()


@mcp.tool()
async def portfolio_remove_manual(label: str) -> dict:
    """Delete a manual position by label (e.g. after fully unstaking)."""
    s = db.SessionLocal()
    try:
        row = s.query(db.ManualPosition).filter(db.ManualPosition.label == label).first()
        if not row:
            return {"ok": False, "error": f"no manual position with label {label!r}"}
        s.delete(row); s.commit()
        return {"ok": True, "removed_label": label}
    finally:
        s.close()


@mcp.tool()
async def portfolio_hide_token(chain: str, token_address: str, symbol: str = "", reason: str = "") -> dict:
    """Mark a token as spam/hidden. Future snapshots exclude it from totals + lists.
    Use chain slug (eth, bsc, cronos, polygon, arbitrum, base, avalanche, zksync-era).
    token_address must be the contract address (0x...). Idempotent."""
    s = db.SessionLocal()
    try:
        existing = (s.query(db.HiddenToken)
                      .filter(db.HiddenToken.chain == chain,
                              db.HiddenToken.token_address == token_address.lower())
                      .first())
        if existing:
            return {"ok": True, "already_hidden": True, "id": existing.id}
        row = db.HiddenToken(
            chain=chain,
            token_address=token_address.lower(),
            symbol=symbol or None,
            reason=reason or None,
            hidden_at=db.now_utc(),
        )
        s.add(row)
        s.commit()
        return {"ok": True, "id": row.id, "chain": chain, "token": token_address.lower(), "symbol": symbol}
    finally:
        s.close()


@mcp.tool()
async def portfolio_list_hidden() -> dict:
    """List all currently hidden/spam-flagged tokens."""
    s = db.SessionLocal()
    try:
        rows = s.query(db.HiddenToken).order_by(db.HiddenToken.hidden_at.desc()).all()
        return {"count": len(rows), "hidden": [
            {"id": r.id, "chain": r.chain, "token_address": r.token_address,
             "symbol": r.symbol, "reason": r.reason, "hidden_at": r.hidden_at.isoformat()}
            for r in rows
        ]}
    finally:
        s.close()


# Health endpoint for Docker healthcheck (non-MCP)
async def _health(_req):
    return JSONResponse({"status": "ok", "service": "portfolio-mcp"})


async def _balance_sheet_html(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    try:
        data = await bs.build_balance_sheet()
        return HTMLResponse(bs.render_html(data))
    except Exception as e:
        logger.exception("balance_sheet HTML build failed")
        return PlainTextResponse(f"balance_sheet error: {e}", status_code=500)


async def _balance_sheet_json(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    try:
        data = await bs.build_balance_sheet()
        return JSONResponse(data)
    except Exception as e:
        logger.exception("balance_sheet JSON build failed")
        return JSONResponse({"error": str(e)}, status_code=500)


# Headers that disable browser back-cache (bfcache) + intermediate caches
# so glance counts always reflect the latest Firefly state after a
# recategorise round-trip. Applied to Home + Pending drill responses.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


async def _root(req):
    """Sentinel Finance home dashboard — gated by auth."""
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    try:
        summary = await home_mod.build_home_summary()
        return HTMLResponse(home_mod.render_home(summary),
                            headers=_NO_CACHE_HEADERS)
    except Exception as e:
        logger.exception("home build failed")
        return PlainTextResponse(f"home error: {e}", status_code=500)


async def _config_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    return HTMLResponse(home_mod.render_config_page(user))


async def _config_connectors_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    flash = req.query_params.get("flash", "")
    try:
        html = await home_mod.render_connectors_page(user, flash=flash)
        return HTMLResponse(html)
    except Exception as e:
        logger.exception("connectors page failed")
        return PlainTextResponse(f"connectors error: {e}", status_code=500)


async def _config_datetime_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import settings as app_settings
    flash = ""
    if req.method == "POST":
        form = await req.form()
        updates: dict = {}
        df = form.get("date_format", "").strip()
        if df in app_settings.DATE_FORMATS:
            updates["date_format"] = df
        tz = form.get("timezone", "").strip()
        if tz in app_settings.TIMEZONES:
            updates["timezone"] = tz
        try:
            rate = float(form.get("youragency_rate", ""))
            factor = float(form.get("pending_factor", ""))
            if rate >= 0 and 0.0 <= factor <= 1.0:
                updates["youragency"] = {
                    "default_pay_per_shift": rate,
                    "pending_factor": factor,
                }
        except (TypeError, ValueError):
            pass
        try:
            dust = float(form.get("dust_usd", ""))
            if dust >= 0:
                updates["dust_usd"] = dust
        except (TypeError, ValueError):
            pass
        if updates:
            app_settings.save(updates)
            flash = "Settings saved."
        else:
            flash = "No valid changes detected."
    return HTMLResponse(home_mod.render_datetime_page(user, flash=flash))


async def _admin_accounts_page(req):
    """Show the account directory + form to register new account numbers."""
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    if (user.role or "").lower() != "admin":
        return PlainTextResponse("admin only", status_code=403)
    from . import account_directory as _adir
    flash = req.query_params.get("flash", "")
    flash_html = (
        f'<div style="background:rgba(76,217,100,0.10);border:1px solid var(--accent);'
        f'color:var(--accent);padding:10px 12px;border-radius:8px;margin:12px 0;font-size:12px;">{flash}</div>'
    ) if flash else ""

    entries = _adir.all_entries()
    s = _adir.stats()

    by_kind: dict[str, list] = {"liability": [], "asset": []}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)

    def _entry_card(e) -> str:
        nums_html = ", ".join(
            f'<code style="font-family:ui-monospace,monospace;font-size:10px;">{n}</code>'
            for n in e.account_numbers[:4]
        )
        if len(e.account_numbers) > 4:
            nums_html += f' <span class="muted">+{len(e.account_numbers) - 4} more</span>'
        return (
            f'<div class="card" style="margin-top:8px;padding:10px 14px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">'
            f'<span class="name"><b>{e.name}</b> '
            f'<span class="muted" style="font-size:10px;">#{e.firefly_account_id}</span></span>'
            f'<span class="amt">{e.kind}</span></div>'
            f'<div class="sub" style="margin-top:4px;">{nums_html}</div>'
            f'<div class="muted" style="font-size:10px;margin-top:2px;">'
            f'Category: {e.category or "—"} · type: {e.account_type}</div>'
            + (f'<div class="muted" style="font-size:10px;margin-top:2px;">{e.notes}</div>' if e.notes else "")
            + '</div>'
        )

    liab_html = "".join(_entry_card(e) for e in by_kind.get("liability", []))
    asset_html = "".join(_entry_card(e) for e in by_kind.get("asset", []))

    add_form = (
        '<details class="card collapse-card" style="margin-top:12px;">'
        '<summary><span class="name"><b>+ Add asset account</b>'
        '<div class="sub">e.g. POSB Savings 170-37376-6, Wise ref 3427002</div></span>'
        '<span class="amt">›</span></summary>'
        '<form method="post" action="/admin/accounts/add" style="padding:10px 12px;display:grid;gap:8px;">'
        '<input type="number" name="firefly_account_id" placeholder="Firefly account ID (e.g. 1)" required '
        'style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;letter-spacing:normal;text-align:left;">'
        '<input type="text" name="name" placeholder="Account name (e.g. POSB Savings)" required '
        'style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;letter-spacing:normal;text-align:left;">'
        '<input type="text" name="account_numbers" placeholder="Account numbers (comma-separated)" required '
        'style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;letter-spacing:normal;text-align:left;">'
        '<button type="submit" style="background:var(--accent);color:#000;border:none;padding:6px 12px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;">Add</button>'
        '</form></details>'
    )

    body = (
        '<a class="back" href="/config">&larr; Back</a>'
        '<h1>Account directory</h1>'
        '<p class="meta">Real-world account numbers → Firefly account mapping. '
        'Pending Reconciliation auto-matches a tx if its description contains any of these numbers.</p>'
        + flash_html +
        f'<div class="card" style="padding:14px 16px;">'
        f'<div class="big">{s["total"]} accounts</div>'
        f'<div class="muted" style="font-size:11px;margin-top:4px;">'
        + " · ".join(f'{c} {k}' for k, c in s.get("by_kind", {}).items()) +
        f'</div></div>'
        + add_form +
        (f'<div class="section-label">Liability accounts ({len(by_kind.get("liability", []))})</div>'
         + liab_html if liab_html else "")
        + (f'<div class="section-label">Asset accounts ({len(by_kind.get("asset", []))})</div>'
           + asset_html if asset_html else
           '<div class="section-label">Asset accounts</div>'
           '<p class="meta" style="text-align:center;padding:14px;">'
           'No asset-account numbers registered yet. Tap "+ Add asset account" above to register POSB / Wise / Maybank / SC numbers.</p>')
        + '<footer>By Azfar · Powered by Claude · Liability accts come from finance/liabilities-registry.yaml</footer>'
    )
    return HTMLResponse(home_mod._layout("Account directory", body))


async def _admin_accounts_add(req):
    """Append a new asset-account entry."""
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    if (user.role or "").lower() != "admin":
        return PlainTextResponse("admin only", status_code=403)
    from . import account_directory as _adir
    from starlette.responses import RedirectResponse
    from urllib.parse import quote
    form = await req.form()
    try:
        fid = int(form.get("firefly_account_id") or 0)
    except ValueError:
        fid = 0
    name = (form.get("name") or "").strip()
    nums_raw = (form.get("account_numbers") or "").strip()
    nums = [n.strip() for n in nums_raw.split(",") if n.strip()]
    if fid <= 0 or not name or not nums:
        return RedirectResponse(
            "/admin/accounts?flash=" + quote("firefly_account_id + name + numbers required"),
            status_code=303)
    result = _adir.add_asset_account(fid, name, nums, account_type="expense")
    msg = (f"{result['action']} · {name} (#{fid}) "
           f"with {len(nums)} number(s)") if result.get("ok") else f"Error: {result}"
    return RedirectResponse(f"/admin/accounts?flash={quote(msg)}", status_code=303)


async def _admin_reconcile_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    if (user.role or "").lower() != "admin":
        return PlainTextResponse("admin only", status_code=403)
    from . import reconcile as _rec
    days_param = req.query_params.get("days", "60")
    try:
        days = max(7, min(365, int(days_param)))
    except (TypeError, ValueError):
        days = 60
    report = await _rec.run_reconcile(days=days)
    spend = await _rec.spend_analysis(days=days)
    t = report["totals"]

    period_form = (
        '<form method="get" action="/admin/reconcile" style="display:flex;gap:8px;align-items:center;margin-bottom:12px;">'
        '<label style="font-size:12px;color:var(--muted);">Window:</label>'
        f'<select name="days" onchange="this.form.submit()">'
        + "".join(f'<option value="{d}"{" selected" if d == days else ""}>{d} days</option>'
                  for d in (30, 60, 90, 180, 365))
        + '</select></form>'
    )

    summary_card = (
        '<div class="card" style="padding:14px 16px;">'
        f'<div class="big">{t["matched_count"]} matched</div>'
        f'<div class="muted" style="font-size:12px;margin-top:4px;">'
        f'POSB → CC pairs · '
        f'<span class="amt pos">SGD {t["matched_sgd"]:,.2f}</span> reconciled</div>'
        f'<div class="muted" style="font-size:11px;margin-top:6px;">'
        f'Unmatched POSB outflows: <b class="neg">{t["unmatched_posb_count"]}</b> '
        f'(<span class="amt">SGD {t["unmatched_posb_sgd"]:,.2f}</span>) · '
        f'Unmatched CC charges: <b class="neg">{t["unmatched_cc_count"]}</b> '
        f'(<span class="amt">SGD {t["unmatched_cc_sgd"]:,.2f}</span>)</div></div>'
    )

    # Hint banner if data appears one-sided (only POSB outflows, no CC charges)
    hint_html = ""
    if t["unmatched_posb_count"] > 0 and t["unmatched_cc_count"] == 0 and t["matched_count"] == 0:
        hint_html = (
            '<div style="background:rgba(255,204,0,0.10);border:1px solid #ffcc00;'
            'border-radius:8px;padding:10px;color:#ffcc00;font-size:12px;margin:12px 0;">'
            f'<b>Heads up:</b> {t["unmatched_posb_count"]} POSB payments to CC accounts found, '
            'but no CC charges loaded for the window. Import each CC statement '
            '(once parsers land for DBS/HSBC/MB/SC/UOB) to close the loop. '
            'Until then, unmatched POSB outflows are just "settlements with no '
            'CC-side ledger entry to match against".'
            '</div>'
        )

    def _pair_card(m: dict) -> str:
        return (
            f'<div class="card" style="margin-top:8px;padding:10px 14px;">'
            f'<div style="display:flex;justify-content:space-between;gap:8px;">'
            f'<span class="name"><b>{m["counterparty"]}</b> '
            f'<span class="muted" style="font-size:11px;">→ {m["cc_account_name"]}</span></span>'
            f'<span class="amt"><b>SGD {m["amount_posb"]:,.2f}</b></span></div>'
            f'<div class="muted" style="font-size:10px;margin-top:2px;">'
            f'POSB {m["posb_date"]} · CC {m["cc_date"]} · '
            f'{abs(m["day_diff"])}d apart · diff '
            f'<span class="amt">SGD {m["amount_diff"]:+.2f}</span></div></div>'
        )

    def _unmatched_card(u: dict, kind: str) -> str:
        cls = "neg" if kind == "posb" else "muted"
        side = "POSB outflow" if kind == "posb" else "CC charge"
        sub_label = u.get("counterparty") or u.get("cc_account_name") or "?"
        return (
            f'<div class="card" style="margin-top:8px;padding:10px 14px;">'
            f'<div style="display:flex;justify-content:space-between;gap:8px;">'
            f'<span class="name">{side} · {u["date"]}'
            f'<div class="muted" style="font-size:10px;">{sub_label}</div></span>'
            f'<span class="amt {cls}"><b>SGD {u["amount"]:,.2f}</b></span></div>'
            f'<div class="muted" style="font-size:10px;font-family:ui-monospace,monospace;margin-top:2px;">'
            f'{u["description"][:120]}</div></div>'
        )

    matched_html = "".join(_pair_card(m) for m in report["matched"][:30])
    unmatched_posb_html = "".join(_unmatched_card(u, "posb") for u in report["unmatched_posb"][:30])
    unmatched_cc_html = "".join(_unmatched_card(u, "cc") for u in report["unmatched_cc"][:30])

    # ── Spend by category section ─────────────────────────────────────────
    def _category_card(c: dict) -> str:
        vendor_chips = " · ".join(
            f'{v["name"]} <span class="amt">${v["sgd"]:,.0f}</span>'
            for v in c["vendors"][:4]
        )
        if len(c["vendors"]) > 4:
            vendor_chips += f' · +{len(c["vendors"]) - 4} more'
        # Color by account_type
        cls = {"income": "pos", "expense": "neg",
               "liability": "neg", "investment": "pos",
               "transfer": "muted"}.get(c.get("account_type"), "")
        return (
            f'<div class="card" style="margin-top:8px;padding:10px 14px;">'
            f'<div style="display:flex;justify-content:space-between;gap:8px;">'
            f'<span class="name"><b>{c["category"]}</b> '
            f'<span class="muted" style="font-size:11px;">· {c["count"]} tx</span></span>'
            f'<span class="amt {cls}"><b>SGD {c["sgd"]:,.2f}</b></span></div>'
            f'<div class="muted" style="font-size:10px;margin-top:4px;">{vendor_chips}</div></div>'
        )

    coverage_pct = spend.get("totals", {}).get("coverage_pct", 0)
    cov_cls = "pos" if coverage_pct >= 80 else ("muted" if coverage_pct >= 50 else "neg")
    gap = spend.get("generic_pdf_gap", {})
    spend_summary_card = (
        '<div class="card" style="padding:14px 16px;">'
        f'<div class="big">SGD {spend.get("totals", {}).get("all_sgd", 0):,.2f}</div>'
        f'<div class="muted" style="font-size:12px;margin-top:4px;">Total POSB outflows in window</div>'
        f'<div class="muted" style="font-size:11px;margin-top:8px;">'
        f'Classified: <span class="amt {cov_cls}">{coverage_pct:.1f}%</span> '
        f'(SGD {spend.get("totals", {}).get("classified_sgd", 0):,.2f}) · '
        f'<b>{len(spend.get("by_category", []))}</b> categories</div>'
        + (
            f'<div class="muted neg" style="font-size:11px;margin-top:4px;">'
            f'Generic PDF descriptions: <b>{gap.get("count", 0)}</b> tx '
            f'(<span class="amt">SGD {gap.get("sgd", 0):,.2f}</span>, '
            f'{gap.get("share_pct", 0):.1f}% — re-import via iBanking to recover vendor)</div>'
            if gap.get("count", 0) > 0 else ""
        )
        + '</div>'
    )
    spend_html = "".join(_category_card(c) for c in spend.get("by_category", [])[:20])

    uncat_real_html = "".join(
        f'<div class="card" style="margin-top:8px;padding:10px 14px;">'
        f'<div style="display:flex;justify-content:space-between;gap:8px;">'
        f'<span class="name" style="font-family:ui-monospace,monospace;font-size:11px;">{u["description"]}</span>'
        f'<span class="amt"><b>SGD {u["sgd"]:,.2f}</b></span></div>'
        f'<div class="muted" style="font-size:10px;margin-top:2px;">'
        f'{u["count"]} tx — add to classifier.yaml</div></div>'
        for u in spend.get("uncategorized_real", [])[:15]
    )

    body = (
        '<a class="back" href="/config">&larr; Back</a>'
        '<h1>Reconcile · POSB ↔ Credit Cards</h1>'
        f'<p class="meta">{report["window"]["start"]} → {report["window"]["end"]} · '
        f'window {report["window"]["days"]}d · ±{int(5)}d / ±$1.00 tolerance</p>'
        + period_form
        + summary_card
        + hint_html
        + '<div class="section-label">Spend by category</div>'
        + spend_summary_card
        + spend_html
        + (f'<div class="section-label neg">Uncategorized vendors ({len(spend.get("uncategorized_real", []))}) — top 15</div>{uncat_real_html}'
           if spend.get("uncategorized_real") else "")
        + (f'<div class="section-label">CC bill-payment matches ({len(report["matched"])})</div>{matched_html}'
           if report["matched"] else "")
        + (f'<div class="section-label neg">Unmatched POSB → CC payments ({len(report["unmatched_posb"])})</div>{unmatched_posb_html}'
           if report["unmatched_posb"] else "")
        + (f'<div class="section-label neg">Unmatched CC charges ({len(report["unmatched_cc"])})</div>{unmatched_cc_html}'
           if report["unmatched_cc"] else "")
        + '<footer>By Azfar · Powered by Claude</footer>'
    )
    return HTMLResponse(home_mod._layout("Reconcile", body))


async def _income_statement_category(req):
    """List transactions in a given income-statement category."""
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import category_drill as _cd
    from . import classifier as _cls

    slug = req.query_params.get("slug", "uncategorised")
    txn_type = req.query_params.get("type", "withdrawal")
    if txn_type not in ("withdrawal", "deposit"):
        txn_type = "withdrawal"
    try:
        year = int(req.query_params.get("year", str(date.today().year)))
    except ValueError:
        year = date.today().year
    month_param = req.query_params.get("month", "")
    month = int(month_param) if month_param.isdigit() and 1 <= int(month_param) <= 12 else None

    data = await _cd.list_category_transactions(slug, txn_type, year, month)
    flash = req.query_params.get("flash", "")
    flash_html = (
        f'<div style="background:rgba(76,217,100,0.10);border:1px solid var(--accent);'
        f'color:var(--accent);padding:10px 12px;border-radius:8px;margin:12px 0;font-size:12px;">{flash}</div>'
    ) if flash else ""

    cat_options = "".join(f'<option value="{c}">{c}</option>'
                          for c in _cls.known_categories())

    from . import account_directory as _adir
    txn_label = "Income" if txn_type == "deposit" else "Expense"
    # Friendly title for the virtual pending bucket
    if slug == "pending":
        page_title = "Pending Reconciliation"
        page_subtitle = "Tx with no category or 'Uncategorised' — needs triage"
    else:
        page_title = f"{txn_label} · {slug.replace('-', ' ').title()}"
        page_subtitle = None

    # v2.27: If Pending bucket, surface PERIOD_DRIFT markers honestly.
    # Drift rows are monthly diagnostic snapshots (GL vs statement CF), not
    # money — see pending_reconciliation_count() docstring. Show: row count,
    # distinct accounts affected, worst single-period drift, link to
    # /reconcile (where drift triage actually lives, not General Expense).
    parked_hint_html = ""
    if slug == "pending":
        from . import category_drill as _cd_local
        parked = await _cd_local.pending_reconciliation_count(days=60)
        dc = parked.get("drift_count", 0)
        da = parked.get("drift_accounts", 0)
        dw = parked.get("drift_worst", 0.0)
        if dc:
            parked_hint_html = (
                f'<div style="background:rgba(255,204,0,0.08);'
                f'border:1px solid rgba(255,204,0,0.30);border-radius:8px;'
                f'padding:10px 12px;margin:12px 0;font-size:12px;color:#ffcc00;">'
                f'<b>{dc} period-drift markers</b> across <b>{da} accounts</b> '
                f'(worst single-period drift: <span class="amt">SGD {dw:,.2f}</span>) — '
                f'GL closing balance disagrees with statement CF. '
                f'<a href="/reconcile" '
                f'style="color:#ffcc00;text-decoration:underline;">'
                f'Open Reconcile to triage ›</a></div>'
            )

    rows_html = ""
    for tx in data.get("transactions", [])[:200]:
        # Use the tx's own type for sign/colour so mixed-type pending lists
        # render income green and expenses red.
        tx_actual_type = (tx.get("type") or txn_type).lower()
        is_deposit = "deposit" in tx_actual_type
        amt_cls = "pos" if is_deposit else "neg"
        sign = "+" if is_deposit else "−"
        # Pre-fill rule suggestion from first 2 words of description
        suggest_pattern = " ".join((tx["description"] or "").split()[:2])[:30].strip()
        suggest_canonical = (tx["destination_name"]
                              if not is_deposit else tx["source_name"])[:40] or suggest_pattern.title()

        # Try matching the description against known account numbers
        matched_acct = _adir.lookup_by_description(tx["description"])
        if matched_acct:
            suggest_canonical = matched_acct.name
            if matched_acct.category:
                suggest_default_cat = matched_acct.category
            else:
                suggest_default_cat = None
            match_chip = (
                f'<div style="background:rgba(76,217,100,0.10);'
                f'border:1px solid var(--accent);border-radius:6px;padding:4px 8px;'
                f'margin-top:4px;color:var(--accent);font-size:10px;display:inline-block;">'
                f'✓ Matched account · {matched_acct.name} '
                f'({matched_acct.category or matched_acct.account_type})</div>'
            )
        else:
            suggest_default_cat = None
            match_chip = ""

        # Counterparty (payer for deposit, recipient for withdrawal)
        counterparty = (tx["source_name"] if is_deposit
                        else tx["destination_name"])
        rows_html += (
            f'<details class="card collapse-card" style="margin-top:8px;">'
            f'<summary>'
            f'<span class="name">{tx["date"]} · <b>{counterparty}</b>'
            f'<div class="sub" style="font-family:ui-monospace,monospace;font-size:10px;">'
            f'{tx["description"][:80]}</div>'
            f'{match_chip}</span>'
            f'<span class="amt {amt_cls}"><b>{sign}SGD {abs(tx["amount"]):,.2f}</b></span>'
            f'</summary>'
            f'<form method="post" action="/income_statement/tx/recategorise" '
            f'style="padding:10px 12px;display:grid;gap:8px;">'
            f'<input type="hidden" name="tx_id" value="{tx["tx_id"]}">'
            f'<input type="hidden" name="journal_id" value="{tx["journal_id"]}">'
            f'<input type="hidden" name="back_slug" value="{slug}">'
            f'<input type="hidden" name="back_type" value="{txn_type}">'
            f'<input type="hidden" name="back_year" value="{year}">'
            + (f'<input type="hidden" name="back_month" value="{month}">' if month else "")
            + f'<label class="muted" style="font-size:10px;">Move to category</label>'
            f'<select name="new_category" style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;">'
            f'{cat_options}</select>'
            f'<label style="font-size:11px;color:var(--muted);display:flex;align-items:center;gap:6px;">'
            f'<input type="checkbox" name="add_rule" value="1" style="width:14px;height:14px;"> '
            f'Also add classifier rule for this description</label>'
            f'<input type="text" name="rule_pattern" value="{suggest_pattern}" '
            f'placeholder="pattern (substring, case-insens)" '
            f'style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;letter-spacing:normal;text-align:left;">'
            f'<input type="text" name="canonical" value="{suggest_canonical}" '
            f'placeholder="canonical name" '
            f'style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;letter-spacing:normal;text-align:left;">'
            f'<button type="submit" style="background:var(--accent);color:#000;border:none;padding:6px 12px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;">'
            f'Save reclassification</button></form></details>'
        )

    t = data.get("totals", {"count": 0, "sgd": 0})
    period_str = data.get("period_label", "")
    back_qs = f"year={year}" + (f"&month={month}" if month else "")
    back_link = ('<a class="back" href="/">&larr; Home</a>'
                 if slug == "pending"
                 else f'<a class="back" href="/income_statement?{back_qs}">&larr; Income Statement</a>')
    body = (
        back_link
        + f'<h1>{page_title}</h1>'
        + (f'<p class="meta">{page_subtitle}</p>' if page_subtitle else "")
        + f'<p class="meta">{period_str} · {t["count"]} transactions · '
        f'<span class="amt">SGD {t["sgd"]:,.2f}</span></p>'
        + flash_html
        + parked_hint_html
        + (rows_html if rows_html else
           '<p class="meta" style="text-align:center;padding:20px;">No transactions in this category for the period.</p>')
        + '<footer>By Azfar · Powered by Claude · Tap any tx to recategorise</footer>'
    )
    # Pending drill: disable bfcache so the count stays in sync with Firefly
    headers = _NO_CACHE_HEADERS if slug == "pending" else None
    return HTMLResponse(home_mod._layout(page_title, body), headers=headers)


async def _income_statement_tx_recategorise(req):
    """Apply a category change to one Firefly tx + optionally append rule."""
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import category_drill as _cd
    from starlette.responses import RedirectResponse
    from urllib.parse import quote, urlencode
    form = await req.form()
    tx_id = (form.get("tx_id") or "").strip()
    journal_id = (form.get("journal_id") or "").strip()
    new_category = (form.get("new_category") or "").strip()
    if not (tx_id and new_category):
        return PlainTextResponse("tx_id + new_category required", status_code=400)
    add_rule = form.get("add_rule") == "1"
    pattern = (form.get("rule_pattern") or "").strip() if add_rule else None
    canonical = (form.get("canonical") or "").strip() if add_rule else None
    # Infer account_type from existing classifier rule for this category
    from . import classifier as _cls
    acct_type = "expense"
    for v in _cls._load():
        if v.get("category") == new_category:
            acct_type = v.get("account_type", "expense")
            break
    result = await _cd.recategorise(tx_id, journal_id, new_category,
                                     add_rule_pattern=pattern,
                                     canonical=canonical,
                                     account_type=acct_type)
    parts = []
    if result.get("ok"):
        parts.append(f"Tx {tx_id} → '{new_category}'")
    else:
        parts.append(f"Tx {tx_id} update failed: {result.get('error') or result.get('status')}")
    if result.get("rule_added"):
        ra = result["rule_added"]
        if ra.get("ok"):
            parts.append(f"rule '{ra.get('canonical')}' {ra.get('action')}")
    flash = " · ".join(parts)

    back_slug = form.get("back_slug") or _cd.slugify(new_category)
    back_type = form.get("back_type") or "withdrawal"
    back_year = form.get("back_year") or str(date.today().year)
    back_qs = urlencode({"slug": back_slug, "type": back_type,
                         "year": back_year, "flash": flash})
    if form.get("back_month"):
        back_qs += f"&month={form.get('back_month')}"
    return RedirectResponse(f"/income_statement/category?{back_qs}",
                            status_code=303)


async def _admin_classifier_edit(req):
    """Add a new classifier rule or bulk-reclassify existing Firefly tx."""
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    if (user.role or "").lower() != "admin":
        return PlainTextResponse("admin only", status_code=403)
    from . import classifier as _cls
    from starlette.responses import RedirectResponse
    from urllib.parse import quote

    if req.method != "POST":
        return RedirectResponse("/admin/classifier", status_code=302)

    form = await req.form()
    action = (form.get("action") or "").strip()

    if action == "add_rule":
        canonical = (form.get("canonical") or "").strip()
        pattern = (form.get("pattern") or "").strip()
        category = (form.get("category") or "").strip() or _cls.DEFAULT_CATEGORY
        acct_type = (form.get("account_type") or "expense").strip()
        result = _cls.add_rule(canonical, pattern, category, acct_type)
        if not result.get("ok"):
            flash = f"Error: {result.get('error')}"
        else:
            flash = (f"{result['action']} · {result['canonical']} "
                     f"({result['pattern']}) → {category}. "
                     f"{result['total_rules']} rules total.")
        return RedirectResponse(f"/admin/classifier?flash={quote(flash)}",
                                status_code=303)

    if action == "reclassify_existing":
        # Bulk update Firefly transactions: for each tx, run classifier.lookup
        # and if it differs from current category, PATCH it.
        days_param = form.get("days", "60")
        try:
            days = max(7, min(365, int(days_param)))
        except (TypeError, ValueError):
            days = 60
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=days)
        pat = os.environ.get("FIREFLY_PAT", "")
        if not pat:
            return RedirectResponse(
                "/admin/classifier?flash=" + quote("FIREFLY_PAT missing"),
                status_code=303)
        h = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
        updated = 0
        skipped = 0
        async with httpx.AsyncClient(timeout=30) as c:
            for page in range(1, 11):
                r = await c.get(
                    f"{os.environ.get('FIREFLY_INTERNAL_URL', 'http://host.docker.internal:8180')}/api/v1/transactions",
                    headers=h, params={"start": start.isoformat(),
                                       "end": end.isoformat(),
                                       "limit": 200, "page": page,
                                       "type": "withdrawal"})
                body = r.json()
                for t in body.get("data", []):
                    tx = t["attributes"]["transactions"][0]
                    desc = tx.get("description") or ""
                    match = _cls.lookup(desc)
                    if not match:
                        continue
                    cur_cat = tx.get("category_name") or ""
                    if cur_cat == match.category:
                        skipped += 1
                        continue
                    # PATCH transaction group to update category
                    tx_id = t["id"]
                    try:
                        pr = await c.put(
                            f"{os.environ.get('FIREFLY_INTERNAL_URL', 'http://host.docker.internal:8180')}/api/v1/transactions/{tx_id}",
                            headers={**h, "Content-Type": "application/json"},
                            json={"apply_rules": False,
                                  "transactions": [{
                                      "transaction_journal_id": tx.get("transaction_journal_id"),
                                      "category_name": match.category,
                                  }]})
                        if pr.status_code in (200, 201):
                            updated += 1
                        else:
                            skipped += 1
                    except Exception:
                        skipped += 1
                meta = body.get("meta", {}).get("pagination", {})
                if page >= int(meta.get("total_pages", 1) or 1):
                    break
        flash = (f"Reclassify: {updated} updated · {skipped} unchanged "
                 f"(window {days}d)")
        return RedirectResponse(f"/admin/classifier?flash={quote(flash)}",
                                status_code=303)

    return RedirectResponse("/admin/classifier", status_code=302)


async def _admin_classifier_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    if (user.role or "").lower() != "admin":
        return PlainTextResponse("admin only", status_code=403)
    from . import classifier as _cls

    # Pull recent descriptions from Firefly to find unmatched ones
    pat = os.environ.get("FIREFLY_PAT", "")
    descriptions: list[str] = []
    if pat:
        try:
            from datetime import date, timedelta
            start = (date.today() - timedelta(days=60)).isoformat()
            end = date.today().isoformat()
            async with httpx.AsyncClient(timeout=20) as c:
                page = 1
                while page <= 5:  # cap at 5 pages = 1000 tx
                    r = await c.get(
                        f"{os.environ.get('FIREFLY_INTERNAL_URL', 'http://host.docker.internal:8180')}/api/v1/transactions",
                        headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
                        params={"start": start, "end": end, "limit": 200, "page": page},
                    )
                    data = r.json()
                    for t in data.get("data", []):
                        tx = t["attributes"]["transactions"][0]
                        desc = tx.get("description") or ""
                        if desc:
                            descriptions.append(desc)
                    meta = data.get("meta", {}).get("pagination", {})
                    if page >= int(meta.get("total_pages", 1) or 1):
                        break
                    page += 1
        except Exception:
            logger.exception("classifier triage: firefly fetch failed")

    unmatched = _cls.unmatched_examples(descriptions, limit=30)
    s = _cls.stats()

    by_type_html = " · ".join(
        f'<span class="amt">{c}</span> {t}'
        for t, c in sorted(s["by_account_type"].items(), key=lambda kv: -kv[1])
    )
    summary_card = (
        '<div class="card" style="padding:14px 16px;">'
        f'<div class="big">{s["vendor_count"]} rules</div>'
        f'<div class="muted" style="font-size:12px;margin-top:4px;">{by_type_html}</div>'
        f'<div class="muted" style="font-size:11px;margin-top:6px;">'
        f'Source: <code>{s["yaml_path"]}</code> · '
        f'Scanned {len(descriptions)} tx over last 60 days · '
        f'<b class="neg">{len(unmatched)}</b> unmatched groups</div></div>'
    )

    cat_options = "".join(f'<option value="{c}">{c}</option>'
                          for c in _cls.known_categories())
    type_options = "".join(f'<option value="{at}"{" selected" if at == "expense" else ""}>{at}</option>'
                           for at in _cls.known_account_types())

    if not unmatched:
        rows_html = (
            '<p class="meta" style="text-align:center;padding:20px;">'
            'All recent descriptions match a classifier rule. Nothing to triage.</p>'
        )
    else:
        rows_html = ""
        for idx, u in enumerate(unmatched):
            esc_desc = u["description"].replace("<", "&lt;").replace(">", "&gt;")
            # Suggest a pattern: first 1-2 words of description
            suggest_pattern = " ".join(u["description"].split()[:2])[:30].strip()
            suggest_canonical = suggest_pattern.title()
            rows_html += (
                f'<details class="card collapse-card" style="margin-top:8px;">'
                f'<summary>'
                f'<span class="name" style="font-family:ui-monospace,monospace;font-size:11px;">{esc_desc}</span>'
                f'<span class="amt"><b>{u["count"]}×</b></span>'
                f'</summary>'
                f'<form method="post" action="/admin/classifier/edit" '
                f'style="padding:10px 12px;display:grid;gap:8px;">'
                f'<input type="hidden" name="action" value="add_rule">'
                f'<input type="text" name="pattern" value="{suggest_pattern}" '
                f'placeholder="match pattern (case-insensitive substring)" '
                f'style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;letter-spacing:normal;text-align:left;">'
                f'<input type="text" name="canonical" value="{suggest_canonical}" '
                f'placeholder="canonical name (e.g. Foodpanda)" '
                f'style="padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;letter-spacing:normal;text-align:left;">'
                f'<div style="display:flex;gap:6px;">'
                f'<select name="category" style="flex:2;padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;">'
                f'{cat_options}</select>'
                f'<select name="account_type" style="flex:1;padding:6px 8px;font-size:12px;background:#1c1c1e;color:var(--fg);border:1px solid var(--sep);border-radius:6px;">'
                f'{type_options}</select>'
                f'<button type="submit" style="background:var(--accent);color:#000;border:none;padding:6px 12px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;">+ Add rule</button>'
                f'</div></form></details>'
            )

    flash = req.query_params.get("flash", "")
    flash_html = (
        f'<div style="background:rgba(76,217,100,0.10);border:1px solid var(--accent);'
        f'color:var(--accent);padding:10px 12px;border-radius:8px;margin:12px 0;font-size:12px;">{flash}</div>'
    ) if flash else ""

    reclassify_form = (
        '<form method="post" action="/admin/classifier/edit" style="margin-top:12px;">'
        '<input type="hidden" name="action" value="reclassify_existing">'
        '<input type="hidden" name="days" value="60">'
        '<button type="submit" style="background:transparent;color:var(--accent);'
        'border:1px solid var(--accent);padding:8px 14px;border-radius:8px;'
        'font-size:12px;font-weight:600;cursor:pointer;">'
        '↻ Apply current rules to last 60d of Firefly tx</button></form>'
    )

    body = (
        '<a class="back" href="/config">&larr; Back</a>'
        '<h1>Counterparty classifier</h1>'
        '<p class="meta">Vendor → canonical name + category + account type. '
        'Single source of truth in <code>finance/classifier.yaml</code>. '
        'Unknown descriptions default to <b>General Expense</b>.</p>'
        + flash_html
        + summary_card
        + reclassify_form +
        '<div class="section-label">Unmatched descriptions (last 60 days)</div>'
        + rows_html +
        '<footer>By Azfar · Powered by Claude · Tap a description to add a classifier rule</footer>'
    )
    return HTMLResponse(home_mod._layout("Classifier triage", body))


async def _admin_privacy_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    if (user.role or "").lower() != "admin":
        return PlainTextResponse("admin only", status_code=403)
    from . import privacy_audit
    report = privacy_audit.run_full_audit()

    def _sev_pill(sev: str) -> str:
        cls = {"blocker": "neg", "warn": "muted", "info": "muted"}[sev]
        return f'<span class="amt {cls}">{sev.upper()}</span>'

    rows = ""
    for f in report["findings"]:
        rows += (
            f'<div class="card" style="margin-top:8px;padding:12px 14px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">'
            f'<span class="name" style="font-weight:600;">{f["title"]}</span>'
            f'{_sev_pill(f["severity"])}</div>'
            f'<div class="muted" style="font-size:11px;margin-top:2px;">{f["detail"]}</div>'
            f'<div class="muted" style="font-size:10px;font-family:ui-monospace,monospace;margin-top:4px;">'
            f'{f["where"]}</div>'
            f'<div style="color:var(--accent);font-size:11px;margin-top:4px;">→ {f["suggest"]}</div>'
            f'</div>'
        )

    s = report["summary"]
    sum_html = (
        f'<div class="card" style="padding:14px 16px;">'
        f'<div class="big">{s["total"]} findings</div>'
        f'<div class="muted" style="font-size:12px;margin-top:4px;">'
        f'<span class="neg">{s["blocker"]} blocker</span> · '
        f'<span class="muted">{s["warn"]} warn</span> · '
        f'<span class="muted">{s["info"]} info</span></div>'
        f'<div class="muted" style="font-size:11px;margin-top:6px;">Ran {report["ran_at"]}</div>'
        f'</div>'
    )

    body = (
        '<a class="back" href="/config">&larr; Back</a>'
        '<h1>Privacy &amp; data-protection audit</h1>'
        '<p class="meta">Multi-tenant readiness scan. See <a href="/static/PRIVACY.md">PRIVACY.md</a> for the full data inventory.</p>'
        + sum_html
        + rows +
        '<footer>By Azfar · Powered by Claude</footer>'
    )
    return HTMLResponse(home_mod._layout("Privacy audit", body))


async def _config_glance_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import settings as app_settings
    flash = ""
    if req.method == "POST":
        form = await req.form()
        new_cfg = []
        for key in app_settings.GLANCE_CATALOG.keys():
            enabled = form.get(f"enabled_{key}") is not None
            try:
                order = int(form.get(f"order_{key}", "99"))
            except (TypeError, ValueError):
                order = 99
            new_cfg.append({"key": key, "enabled": enabled, "order": order})
        app_settings.save({"glance_cards": new_cfg})
        flash = "Saved. Glance cards updated."
    return HTMLResponse(home_mod.render_glance_page(user, flash=flash))


async def _imports_history_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import imports_page
    flash = req.query_params.get("flash", "")
    return HTMLResponse(imports_page.render_imports_page(user, flash=flash))


async def _run_csv_import(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import posb_ibanking_importer as imp
    from starlette.responses import RedirectResponse
    from urllib.parse import quote
    try:
        report = imp.scan_and_import(move_after=True)
        if report.get("error"):
            flash = f"Import failed: {report['error']}"
        elif report["scanned"] == 0:
            flash = "No CSV files found in Auto-import drop folders."
        else:
            parts = []
            for r in report["results"]:
                if r.get("error"):
                    parts.append(f"{r['file']}: {r['error']}")
                else:
                    parts.append(
                        f"{r['file']}: {r['created']} created · "
                        f"{r['dup']} dup · {r['errored']} err "
                        f"({r['account_name']})"
                    )
            flash = " | ".join(parts)
        return RedirectResponse(f"/config/connectors?flash={quote(flash)}", status_code=303)
    except Exception as e:
        logger.exception("CSV import failed")
        return PlainTextResponse(f"import error: {e}", status_code=500)


async def _provision_folders(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import folder_provisioning as fp
    try:
        result = fp.provision_all()
        from starlette.responses import RedirectResponse
        # Build a short summary for the flash
        parts = []
        for backend, r in result.items():
            label = "Google Drive" if backend == "google_drive" else "OneDrive"
            if r.get("ok"):
                created = sum(1 for f in r["folders"] if f["status"] == "created")
                exists = sum(1 for f in r["folders"] if f["status"] == "exists")
                parts.append(f"{label}: {created} created · {exists} already existed")
            else:
                parts.append(f"{label}: skipped ({r.get('error','unknown')})")
        flash = " · ".join(parts)
        from urllib.parse import quote
        return RedirectResponse(f"/config/connectors?flash={quote(flash)}", status_code=303)
    except Exception as e:
        logger.exception("folder provisioning failed")
        return PlainTextResponse(f"provision error: {e}", status_code=500)


async def _config_fx_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    from . import fx as fx_mod

    if req.method == "POST":
        form = await req.form()
        action = (form.get("action") or "").strip()
        source = (form.get("source") or "manual").strip()
        try:
            rate = float((form.get("rate") or "0").strip())
        except ValueError:
            return HTMLResponse(home_mod.render_fx_page(user, error="Rate must be a number"), status_code=400)
        if source not in fx_mod.SOURCES:
            return HTMLResponse(home_mod.render_fx_page(user, error=f"Unknown source: {source}"), status_code=400)

        if action == "fetch" and source != "manual":
            fetched, err = await fx_mod.fetch_rate(source)
            if fetched is None:
                return HTMLResponse(home_mod.render_fx_page(user, error=f"Fetch from {source} failed: {err}"), status_code=502)
            new_state = fx_mod.save_fx(fetched, source)
            return HTMLResponse(home_mod.render_fx_page(user,
                flash=f"Fetched {fetched:.4f} from {source} and saved."))
        # action == "save" (or any non-fetch)
        new_state = fx_mod.save_fx(rate, source)
        return HTMLResponse(home_mod.render_fx_page(user, flash=f"Saved: 1 USD = SGD {rate:.4f} ({source})."))

    return HTMLResponse(home_mod.render_fx_page(user))


async def _income_statement_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    try:
        year_param = req.query_params.get("year", "")
        month_param = req.query_params.get("month", "")
        year = int(year_param) if year_param.isdigit() else None
        month = int(month_param) if month_param.isdigit() and 1 <= int(month_param) <= 12 else None
        data = await is_mod.build_income_statement(year, month=month)
        years = await is_mod.available_years()
        return HTMLResponse(is_mod.render_html(data, years, current_month=month))
    except Exception as e:
        logger.exception("income_statement build failed")
        return PlainTextResponse(f"income_statement error: {e}", status_code=500)


async def _income_statement_json(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    year_param = req.query_params.get("year", "")
    year = int(year_param) if year_param.isdigit() else None
    data = await is_mod.build_income_statement(year)
    return JSONResponse(data)


async def _cash_forecast_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    show_form = bool(req.query_params.get("add"))
    flash = req.query_params.get("flash", "")
    try:
        data = await cf_mod.build_forecast(horizon_days=90)
        return HTMLResponse(cf_mod.render_forecast(data, show_form=show_form, flash=flash))
    except Exception as e:
        logger.exception("cash_forecast build failed")
        return PlainTextResponse(f"cash_forecast error: {e}", status_code=500)


async def _cash_forecast_add(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    form = await req.form()
    try:
        cf_mod.add_recurring(
            kind=(form.get("kind") or "").strip(),
            name=(form.get("name") or "").strip(),
            amount=float((form.get("amount") or "0").strip()),
            day=int((form.get("day") or "1").strip()),
            category=(form.get("category") or "").strip(),
            note=(form.get("note") or "").strip(),
        )
        return RedirectResponse(f"/cash_forecast?flash=Added+to+schedule", status_code=302)
    except Exception as e:
        logger.exception("cash_forecast add failed")
        return RedirectResponse(f"/cash_forecast?flash=Error:+{str(e)[:60]}", status_code=302)


async def _drill_page(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    key = req.path_params.get("key", "")
    try:
        if key == "bank":
            data = await drill_mod.build_bank_drill()
            return HTMLResponse(drill_mod.render_bank(data))
        if key == "crypto":
            data = await drill_mod.build_crypto_drill()
            return HTMLResponse(drill_mod.render_crypto(data))
        if key == "loans":
            data = await drill_mod.build_liability_drill(only_type="loans")
            return HTMLResponse(drill_mod.render_liability(data))
        if key == "cc":
            data = await drill_mod.build_liability_drill(only_type="credit_card")
            return HTMLResponse(drill_mod.render_liability(data))
        if key == "recurring":
            data = await drill_mod.build_recurring_drill()
            return HTMLResponse(drill_mod.render_recurring(data))
        if key == "funds":
            data = await drill_mod.build_funds_drill()
            return HTMLResponse(drill_mod.render_funds(data))
        if key == "ilp":
            data = await drill_mod.build_ilp_drill()
            return HTMLResponse(drill_mod.render_ilp(data))
        if key == "cpf":
            data = await drill_mod.build_cpf_drill()
            return HTMLResponse(drill_mod.render_cpf(data))
        if key == "pending":
            # Virtual `pending` bucket = '' + 'Uncategorised' + 'General Expense'
            from starlette.responses import RedirectResponse
            year = date.today().year
            return RedirectResponse(
                f"/income_statement/category?slug=pending&type=withdrawal&year={year}",
                status_code=302)
        return PlainTextResponse(f"Unknown drill key: {key}", status_code=404)
    except Exception as e:
        logger.exception("drill build failed")
        return PlainTextResponse(f"drill error: {e}", status_code=500)


async def _coming_soon(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    page = req.query_params.get("p", "")
    return HTMLResponse(home_mod.render_coming_soon(page))


async def _admin_action_factory(action):
    async def _h(req):
        return await auth.admin_user_action(req, action)
    return _h


async def _admin_approve(req): return await auth.admin_user_action(req, "approve")
async def _admin_deny(req):    return await auth.admin_user_action(req, "deny")
async def _admin_suspend(req): return await auth.admin_user_action(req, "suspend")


STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


async def _assetlinks(_req):
    """Digital Asset Links file for TWA verification. Must be publicly reachable
    (Cloudflare Access bypass required)."""
    path = os.path.join(STATIC_DIR, "assetlinks.json")
    return FileResponse(path, media_type="application/json")


async def _manifest(req):
    """Serve the manifest with absolute URLs anchored to the production host.
    PWA_HOST_OVERRIDE env var or default value is used so Bubblewrap builds the
    TWA against the public hostname regardless of where the manifest is fetched."""
    import json
    base = os.environ.get("PWA_HOST_OVERRIDE", "https://sentinelfinance.your-domain.example.com").rstrip("/")
    path = os.path.join(STATIC_DIR, "manifest.webmanifest")
    with open(path) as f:
        m = json.load(f)
    # Respect whatever start_url the static manifest declares; just absolutise it
    rel_start = m.get("start_url", "/").lstrip("/")
    m["start_url"] = f"{base}/{rel_start}"
    m["scope"] = f"{base}/"
    for icon in m.get("icons", []):
        if icon["src"].startswith("/"):
            icon["src"] = f"{base}{icon['src']}"
    return JSONResponse(m, media_type="application/manifest+json")


async def _sw(_req):
    path = os.path.join(STATIC_DIR, "sw.js")
    return FileResponse(path, media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})


app = mcp.streamable_http_app()

# Auth routes (public)
app.router.routes.insert(0, Route("/auth/login", auth.login_page, methods=["GET"]))
app.router.routes.insert(0, Route("/auth/telegram/callback", auth.telegram_callback, methods=["GET"]))
app.router.routes.insert(0, Route("/auth/logout", auth.logout, methods=["GET", "POST"]))
app.router.routes.insert(0, Route("/auth/pending", auth.pending_page, methods=["GET"]))
app.router.routes.insert(0, Route("/auth/denied", auth.denied_page, methods=["GET"]))
app.router.routes.insert(0, Route("/auth/totp/setup", auth.totp_setup, methods=["GET", "POST"]))
app.router.routes.insert(0, Route("/auth/totp/challenge", auth.totp_challenge, methods=["GET", "POST"]))

# Admin routes (gated inside the handlers)
async def _admin_credit_utilization(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    rows, agg = credit_utilization.compute_utilization()
    return HTMLResponse(credit_utilization.render_html(rows, agg))


async def _agent_credit_utilization(req):
    deny = agent_api._require_agent(req)
    if deny:
        return deny
    rows, agg = credit_utilization.compute_utilization()
    return JSONResponse({
        "facilities": [r.to_dict() for r in rows],
        "aggregate": agg,
    })

app.router.routes.insert(0, Route("/admin/credit_utilization", _admin_credit_utilization, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/credit_utilization", _agent_credit_utilization, methods=["GET"]))


async def _admin_chart_of_accounts(req):
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    return HTMLResponse(coa_view.render_html())


async def _agent_chart_of_accounts(req):
    deny = agent_api._require_agent(req)
    if deny:
        return deny
    return JSONResponse({"accounts": coa_view.build_tree()})

app.router.routes.insert(0, Route("/admin/chart_of_accounts", _admin_chart_of_accounts, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/chart_of_accounts", _agent_chart_of_accounts, methods=["GET"]))
app.router.routes.insert(0, Route("/admin/users", auth.admin_users, methods=["GET"]))
app.router.routes.insert(0, Route("/admin/privacy", _admin_privacy_page, methods=["GET"]))
app.router.routes.insert(0, Route("/admin/classifier", _admin_classifier_page, methods=["GET"]))
app.router.routes.insert(0, Route("/admin/classifier/edit", _admin_classifier_edit, methods=["POST"]))
app.router.routes.insert(0, Route("/admin/reconcile", _admin_reconcile_page, methods=["GET"]))
app.router.routes.insert(0, Route("/admin/accounts", _admin_accounts_page, methods=["GET"]))
app.router.routes.insert(0, Route("/admin/accounts/add", _admin_accounts_add, methods=["POST"]))
app.router.routes.insert(0, Route("/admin/users/{uid:int}/approve", _admin_approve, methods=["POST"]))
app.router.routes.insert(0, Route("/admin/users/{uid:int}/deny", _admin_deny, methods=["POST"]))
app.router.routes.insert(0, Route("/admin/users/{uid:int}/suspend", _admin_suspend, methods=["POST"]))

# App routes
app.router.routes.insert(0, Route("/", _root, methods=["GET"]))
app.router.routes.insert(0, Route("/config", _config_page, methods=["GET"]))
app.router.routes.insert(0, Route("/config/fx", _config_fx_page, methods=["GET", "POST"]))
app.router.routes.insert(0, Route("/config/connectors", _config_connectors_page, methods=["GET"]))
app.router.routes.insert(0, Route("/config/connectors/provision", _provision_folders, methods=["POST"]))
app.router.routes.insert(0, Route("/config/connectors/import-csv", _run_csv_import, methods=["POST"]))
app.router.routes.insert(0, Route("/config/datetime", _config_datetime_page, methods=["GET", "POST"]))
app.router.routes.insert(0, Route("/config/imports", _imports_history_page, methods=["GET"]))
app.router.routes.insert(0, Route("/config/glance", _config_glance_page, methods=["GET", "POST"]))
app.router.routes.insert(0, Route("/income_statement", _income_statement_page, methods=["GET"]))
app.router.routes.insert(0, Route("/income_statement/category", _income_statement_category, methods=["GET"]))
app.router.routes.insert(0, Route("/income_statement/tx/recategorise", _income_statement_tx_recategorise, methods=["POST"]))
app.router.routes.insert(0, Route("/income_statement.json", _income_statement_json, methods=["GET"]))
app.router.routes.insert(0, Route("/drill/{key:str}", _drill_page, methods=["GET"]))
app.router.routes.insert(0, Route("/cash_forecast", _cash_forecast_page, methods=["GET"]))
app.router.routes.insert(0, Route("/cash_forecast/add", _cash_forecast_add, methods=["POST"]))
app.router.routes.insert(0, Route("/coming-soon", _coming_soon, methods=["GET"]))
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
app.router.routes.insert(0, Route("/balance_sheet", _balance_sheet_html, methods=["GET"]))
app.router.routes.insert(0, Route("/balance_sheet.json", _balance_sheet_json, methods=["GET"]))


# V2 dashboards — pre-posting verifier surface + canonical registries
async def _reconcile_page(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    return HTMLResponse(v2_dashboards.render_reconcile_page())


async def _reconcile_post(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    qid = int(req.path_params["qid"])
    form = await req.form()
    coa = (form.get("contra_coa") or "").strip() or None
    ok, msg = v2_dashboards.resolve_post(qid, coa)
    logger.info("reconcile post qid=%s ok=%s msg=%s", qid, ok, msg)
    return RedirectResponse(url="/reconcile", status_code=303)


async def _reconcile_reject(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    qid = int(req.path_params["qid"])
    ok, msg = v2_dashboards.resolve_reject(qid)
    logger.info("reconcile reject qid=%s ok=%s msg=%s", qid, ok, msg)
    return RedirectResponse(url="/reconcile", status_code=303)


async def _reconcile_triage(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    qid = int(req.path_params["qid"])
    form = await req.form()
    category = (form.get("category") or "").strip()
    ok, msg = v2_dashboards.resolve_triage(qid, category)
    logger.info("reconcile triage qid=%s category=%s ok=%s msg=%s",
                qid, category, ok, msg)
    return RedirectResponse(url="/reconcile", status_code=303)


async def _suspense_page(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    return HTMLResponse(v2_dashboards.render_suspense_page())


async def _suspense_apply_high(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    ok, msg = v2_dashboards.apply_suspense_high()
    logger.info("suspense apply_high ok=%s msg=%s", ok, msg)
    return RedirectResponse(url="/reconcile/suspense", status_code=303)


async def _alerts_page(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    return HTMLResponse(v2_dashboards.render_alerts_page())


async def _alert_resolve(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    aid = int(req.path_params["aid"])
    ok, msg = v2_dashboards.alert_resolve(aid)
    logger.info("alert resolve aid=%s ok=%s msg=%s", aid, ok, msg)
    return RedirectResponse(url="/alerts", status_code=303)


async def _alert_dismiss(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    aid = int(req.path_params["aid"])
    ok, msg = v2_dashboards.alert_dismiss(aid)
    logger.info("alert dismiss aid=%s ok=%s msg=%s", aid, ok, msg)
    return RedirectResponse(url="/alerts", status_code=303)


async def _facilities_page(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    return HTMLResponse(v2_dashboards.render_facilities_page())


async def _policies_page(req):
    try:
        auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    return HTMLResponse(v2_dashboards.render_policies_page())


app.router.routes.insert(0, Route("/reconcile", _reconcile_page, methods=["GET"]))
app.router.routes.insert(0, Route("/reconcile/{qid:int}/post", _reconcile_post, methods=["POST"]))
app.router.routes.insert(0, Route("/reconcile/{qid:int}/reject", _reconcile_reject, methods=["POST"]))
app.router.routes.insert(0, Route("/reconcile/{qid:int}/triage", _reconcile_triage, methods=["POST"]))
app.router.routes.insert(0, Route("/facilities", _facilities_page, methods=["GET"]))
app.router.routes.insert(0, Route("/policies", _policies_page, methods=["GET"]))
app.router.routes.insert(0, Route("/reconcile/suspense", _suspense_page, methods=["GET"]))
app.router.routes.insert(0, Route("/reconcile/suspense/apply_high", _suspense_apply_high, methods=["POST"]))
app.router.routes.insert(0, Route("/alerts", _alerts_page, methods=["GET"]))
app.router.routes.insert(0, Route("/alerts/{aid:int}/resolve", _alert_resolve, methods=["POST"]))
app.router.routes.insert(0, Route("/alerts/{aid:int}/dismiss", _alert_dismiss, methods=["POST"]))

# Sentinel AI service-token surface — bearer-gated, read-only JSON.
# See app/agent_api.py and workspace/tools/sentinel-finance.md for the contract.
app.router.routes.insert(0, Route("/api/agent/health", agent_api.agent_health, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/balance_sheet", agent_api.agent_balance_sheet, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/income_statement", agent_api.agent_income_statement, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/pending_count", agent_api.agent_pending_count, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/cash_forecast", agent_api.agent_cash_forecast, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/classifier/lookup", agent_api.agent_classifier_lookup, methods=["GET"]))
app.router.routes.insert(0, Route("/api/agent/glance", agent_api.agent_glance, methods=["GET"]))

# PWA / TWA assets — public (no auth)
app.router.routes.insert(0, Route("/.well-known/assetlinks.json", _assetlinks, methods=["GET"]))
app.router.routes.insert(0, Route("/manifest.webmanifest", _manifest, methods=["GET"]))
app.router.routes.insert(0, Route("/sw.js", _sw, methods=["GET"]))
app.router.routes.insert(0, Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"))


# Self-bootstrap: trigger the FastMCP lifespan after uvicorn comes up, so the
# scheduler + Telegram bot listener start without needing MetaMCP to make the
# first MCP call. Adds an asyncio task that sleeps briefly then opens a session
# against ourselves.
async def _kick_lifespan():
    await asyncio.sleep(2)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "http://127.0.0.1:8086/mcp",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "self-bootstrap", "version": "1"}}},
            )
            logger.info("self-bootstrap MCP init: HTTP %d", r.status_code)
    except Exception:
        logger.exception("self-bootstrap failed; lifespan will fire on first external MCP call")


_orig_lifespan = app.router.lifespan_context

from contextlib import asynccontextmanager as _acm

@_acm
async def _combined_lifespan(asgi_app):
    async with _orig_lifespan(asgi_app):
        task = asyncio.create_task(_kick_lifespan())
        try:
            yield
        finally:
            task.cancel()

app.router.lifespan_context = _combined_lifespan
