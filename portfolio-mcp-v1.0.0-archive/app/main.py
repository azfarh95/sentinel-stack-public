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
from datetime import timedelta

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse, FileResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from decimal import Decimal

from . import auth
from . import balance_sheet as bs
from . import bot as tg_bot
from . import database as db
from . import dexscreener
from . import cash_forecast as cf_mod
from . import drill as drill_mod
from . import home as home_mod
from . import income_statement as is_mod
from . import wise as wise_mod
from . import moralis
from . import polling
from . import wolfswap

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_ADDR = os.environ.get("PORTFOLIO_DEFAULT_ADDRESS", "")
DUST = float(os.environ.get("PORTFOLIO_DUST_USD", "0.01"))
POLL_INTERVAL_MIN = int(os.environ.get("ONCHAIN_POLL_INTERVAL_MIN", "5"))

scheduler = AsyncIOScheduler(timezone="UTC")


async def _poll_job():
    try:
        n = await polling.poll_once(DEFAULT_ADDR)
        if n:
            logger.info("poll: sent %d telegram alert(s)", n)
    except Exception:
        logger.exception("poll cycle failed")


async def _refresh_wolfswap_amount(row, user_addr: str) -> None:
    """Read live staked PACK amount from WolfSwap proxy. Mutates row.token_amount."""
    info = await wolfswap.get_staking_info(user_addr)
    new_amount = info["total_staked_pack"]
    if new_amount > 0:
        row.token_amount = f"{new_amount:.6f}"
        logger.info("wolfswap amount refresh: %s = %s PACK staked (+%s pending)",
                    row.label, new_amount, info["pending_rewards_pack"])


async def _wise_sync_job():
    """Daily: pull Wise balances + update the Firefly Wise asset account."""
    try:
        result = await wise_mod.sync_now()
        logger.info("wise sync: total SGD %.2f across %d currencies (acct %d)",
                    result["total_sgd"], len(result["currencies"]), result["firefly_account_id"])
        # Invalidate balance-sheet cache so next request sees the updated Wise account
        try:
            bs.invalidate_snapshot_cache()
        except Exception:
            pass
    except Exception:
        logger.exception("wise sync failed")


async def _refresh_manual_prices_job():
    """For each auto-priced manual position:
    1. If it's a WolfSwap row (protocol=WolfSwap, token=PACK), refresh AMOUNT from on-chain.
    2. Refresh PRICE from DexScreener.
    3. Recompute usd_value = amount × price."""
    s = db.SessionLocal()
    try:
        rows = (s.query(db.ManualPosition)
                  .filter(db.ManualPosition.token_address.isnot(None),
                          db.ManualPosition.token_amount.isnot(None)).all())
        updated = 0
        for r in rows:
            try:
                # Step 1: WolfSwap-specific on-chain amount refresh
                if (r.protocol or "").lower() == "wolfswap":
                    await _refresh_wolfswap_amount(r, DEFAULT_ADDR)
                # Step 2: DexScreener price
                price = await dexscreener.token_price(r.token_chain or r.chain, r.token_address)
                amount = Decimal(r.token_amount)
                new_usd = float(amount * Decimal(str(price["price_usd"])))
                old_usd = r.usd_value
                r.last_price_usd = price["price_usd"]
                r.last_priced_at = db.now_utc()
                r.usd_value = new_usd
                r.updated_at = db.now_utc()
                logger.info("position refresh: %s — %s tok × $%s = $%.2f (was $%.2f)",
                            r.label, r.token_amount, price["price_usd"], new_usd, old_usd)
                updated += 1
            except Exception as e:
                logger.warning("position refresh failed for %s: %s", r.label, e)
        if updated:
            s.commit()
    finally:
        s.close()


@asynccontextmanager
async def _lifespan(server: FastMCP):
    db.init_db()
    logger.info("portfolio-mcp ready (dust threshold=$%s)", DUST)
    if DEFAULT_ADDR and not scheduler.running:
        # First run: bookmark current head without alerting historical txs
        try:
            await polling.initialize_bookmarks(DEFAULT_ADDR)
        except Exception:
            logger.exception("bookmark init failed; continuing")
        scheduler.add_job(_poll_job, "interval", minutes=POLL_INTERVAL_MIN,
                          id="onchain_poll", replace_existing=True)
        # Hourly price refresh for auto-priced manual positions (DexScreener).
        # Cheap (1 call per position, free API). Hourly is overkill but harmless.
        scheduler.add_job(_refresh_manual_prices_job, "interval", minutes=60,
                          id="manual_price_refresh", replace_existing=True)
        # Daily Wise sync — pulls multi-currency balances + updates the Wise asset
        # account in Firefly. Only runs if WISE_API_TOKEN is configured.
        if os.environ.get("WISE_API_TOKEN"):
            from apscheduler.triggers.cron import CronTrigger
            scheduler.add_job(_wise_sync_job, CronTrigger(hour=6, minute=30),
                              id="wise_sync", replace_existing=True)
            logger.info("Wise sync scheduled — daily 06:30 + on-demand via wise_sync MCP tool")
        # Run once on startup so first daily snapshot has fresh prices
        try:
            await _refresh_manual_prices_job()
        except Exception:
            logger.exception("initial price refresh failed; continuing")
        scheduler.start()
        logger.info("schedulers started (onchain poll %dm, price refresh 60m)", POLL_INTERVAL_MIN)
        # Bot listener disabled by default — set BOT_LISTENER_ENABLED=1 to re-enable.
        # Polling-based getUpdates loops were producing constant Conflict errors
        # even with an asyncio lock around start_bot. Probable root cause is
        # python-telegram-bot v21 retry behaviour racing with concurrent
        # MCP-session-triggered lifespan invocations. Deferred; outbound
        # notifier.send() still works for daily snapshot reports.
        if os.environ.get("BOT_LISTENER_ENABLED", "0") == "1":
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
    snap = await moralis.wallet_snapshot(addr, dust_threshold_usd=DUST)
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
    snap["manual_positions"] = [
        {"label": r.label, "chain": r.chain, "protocol": r.protocol, "usd_value": r.usd_value}
        for r in manual_rows
    ]
    snap["manual_usd"] = round(sum(r.usd_value for r in manual_rows), 2)
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


async def _root(req):
    """Sentinel Finance home dashboard — gated by auth."""
    try:
        user = auth.require_user(req)
    except auth.AuthRedirect as e:
        return e.response
    try:
        summary = await home_mod.build_home_summary()
        return HTMLResponse(home_mod.render_home(summary))
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
    try:
        html = await home_mod.render_connectors_page(user)
        return HTMLResponse(html)
    except Exception as e:
        logger.exception("connectors page failed")
        return PlainTextResponse(f"connectors error: {e}", status_code=500)


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
        year = int(year_param) if year_param.isdigit() else None
        data = await is_mod.build_income_statement(year)
        years = await is_mod.available_years()
        return HTMLResponse(is_mod.render_html(data, years))
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
app.router.routes.insert(0, Route("/admin/users", auth.admin_users, methods=["GET"]))
app.router.routes.insert(0, Route("/admin/users/{uid:int}/approve", _admin_approve, methods=["POST"]))
app.router.routes.insert(0, Route("/admin/users/{uid:int}/deny", _admin_deny, methods=["POST"]))
app.router.routes.insert(0, Route("/admin/users/{uid:int}/suspend", _admin_suspend, methods=["POST"]))

# App routes
app.router.routes.insert(0, Route("/", _root, methods=["GET"]))
app.router.routes.insert(0, Route("/config", _config_page, methods=["GET"]))
app.router.routes.insert(0, Route("/config/fx", _config_fx_page, methods=["GET", "POST"]))
app.router.routes.insert(0, Route("/config/connectors", _config_connectors_page, methods=["GET"]))
app.router.routes.insert(0, Route("/income_statement", _income_statement_page, methods=["GET"]))
app.router.routes.insert(0, Route("/income_statement.json", _income_statement_json, methods=["GET"]))
app.router.routes.insert(0, Route("/drill/{key:str}", _drill_page, methods=["GET"]))
app.router.routes.insert(0, Route("/cash_forecast", _cash_forecast_page, methods=["GET"]))
app.router.routes.insert(0, Route("/cash_forecast/add", _cash_forecast_add, methods=["POST"]))
app.router.routes.insert(0, Route("/coming-soon", _coming_soon, methods=["GET"]))
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
app.router.routes.insert(0, Route("/balance_sheet", _balance_sheet_html, methods=["GET"]))
app.router.routes.insert(0, Route("/balance_sheet.json", _balance_sheet_json, methods=["GET"]))

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
