"""Telegram bot listener for @YourSentinelBot. Long-polls Telegram for commands
addressed to the owner's DM. Currently exposes:

  /start             Help text listing all commands.
  /wallet_snapshot   Current Moralis snapshot of the watched wallet.
  /wallet_diff       24h delta vs previous snapshot.

Auth: only OWNER_CHAT_ID may invoke. Any other chat sees a polite refusal.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from . import database as db
from . import moralis

logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID"))
DEFAULT_ADDR = os.environ.get("PORTFOLIO_DEFAULT_ADDRESS", "")
DUST = float(os.environ.get("PORTFOLIO_DUST_USD", "0.01"))


def _owner_only(handler):
    """Decorator: silently ignore non-owner chats (or polite refusal)."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat is None or update.effective_chat.id != OWNER_CHAT_ID:
            if update.effective_chat:
                await update.effective_chat.send_message(
                    "This bot is owner-only.", parse_mode=ParseMode.HTML)
            return
        return await handler(update, context)
    return wrapper


@_owner_only
async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>Sentinel Finance bot</b>\n\n"
        "<b>/wallet_snapshot</b> — current Web3 portfolio across 7 chains\n"
        "<b>/wallet_diff</b> — 24h change vs previous snapshot\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)


@_owner_only
async def cmd_wallet_snapshot(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if not DEFAULT_ADDR:
        await update.message.reply_text("No default wallet configured.")
        return
    await update.message.chat.send_action("typing")
    try:
        snap = await moralis.wallet_snapshot(DEFAULT_ADDR, dust_threshold_usd=DUST)
    except Exception as e:
        await update.message.reply_text(f"Snapshot failed: {e}")
        return

    s = db.SessionLocal()
    try:
        hidden = {(r.chain, (r.token_address or "").lower())
                  for r in s.query(db.HiddenToken).all()}
        manual = s.query(db.ManualPosition).all()
    finally:
        s.close()

    snap["positions"] = [p for p in snap["positions"]
                         if (p["chain"], (p["token_address"] or "").lower()) not in hidden]
    liquid = sum(p["usd_value"] for p in snap["positions"])
    manual_usd = sum(r.usd_value for r in manual)
    total = liquid + manual_usd

    top = sorted(snap["positions"], key=lambda x: -x["usd_value"])[:8]
    top_lines = "\n".join(
        f"  <code>{p['chain']:<10}{p['symbol']:<14} ${p['usd_value']:>10,.2f}</code>"
        for p in top
    )
    manual_lines = "\n".join(
        f"  <code>{m.protocol or 'Manual':<10}{m.label[:18]:<19} ${m.usd_value:>10,.2f}</code>"
        for m in sorted(manual, key=lambda x: -x.usd_value)
    ) or "  <code>(none)</code>"

    body = (
        f"<b>Wallet snapshot</b>\n"
        f"<code>{DEFAULT_ADDR[:6]}...{DEFAULT_ADDR[-4:]}</code>\n\n"
        f"<b>Total: ${total:,.2f}</b>\n"
        f"  Liquid: ${liquid:,.2f} ({len(snap['positions'])} positions)\n"
        f"  Manual: ${manual_usd:,.2f} ({len(manual)} positions)\n\n"
        f"<b>Top liquid:</b>\n{top_lines}\n\n"
        f"<b>Manual DeFi:</b>\n{manual_lines}\n\n"
        f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>"
    )
    await update.message.reply_text(body, parse_mode=ParseMode.HTML)


@_owner_only
async def cmd_wallet_diff(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    from datetime import timedelta
    addr = DEFAULT_ADDR.lower()
    s = db.SessionLocal()
    try:
        latest = (s.query(db.Snapshot).filter(db.Snapshot.address == addr)
                    .order_by(db.Snapshot.captured_at.desc()).first())
        cutoff = db.now_utc() - timedelta(days=1)
        prev = (s.query(db.Snapshot)
                  .filter(db.Snapshot.address == addr,
                          db.Snapshot.captured_at <= cutoff)
                  .order_by(db.Snapshot.captured_at.desc()).first())
        if not latest or not prev:
            await update.message.reply_text("Need at least one snapshot before and after 24h ago. Run the daily job a couple of days, then try again.")
            return
        delta = latest.total_usd - prev.total_usd
        arrow = "+" if delta > 0 else ""
        body = (
            f"<b>24h change</b>\n\n"
            f"  Now:  ${latest.total_usd:,.2f}  ({latest.captured_at:%Y-%m-%d %H:%M})\n"
            f"  Then: ${prev.total_usd:,.2f}  ({prev.captured_at:%Y-%m-%d %H:%M})\n"
            f"  <b>Delta: {arrow}${delta:,.2f}</b>"
        )
        await update.message.reply_text(body, parse_mode=ParseMode.HTML)
    finally:
        s.close()


_application: Application | None = None
_start_lock = asyncio.Lock()


async def start_bot():
    """Start long-polling. Idempotent: returns existing app if already running.
    Lock-protected so concurrent lifespan invocations can't race."""
    global _application
    async with _start_lock:
        if _application is not None:
            return _application
        if not TOKEN:
            logger.warning("TELEGRAM_BOT_TOKEN unset — bot listener disabled")
            return None

        app = Application.builder().token(TOKEN).concurrent_updates(True).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_start))
        app.add_handler(CommandHandler("wallet_snapshot", cmd_wallet_snapshot))
        app.add_handler(CommandHandler("wallet_diff", cmd_wallet_diff))

        _application = app  # claim before awaits so racers see non-None
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot listener started (@YourSentinelBot, owner=%d)", OWNER_CHAT_ID)
            return app
        except Exception:
            _application = None  # release claim on failure
            raise


async def stop_bot():
    global _application
    if _application is None:
        return
    try:
        await _application.updater.stop()
        await _application.stop()
        await _application.shutdown()
    except Exception:
        logger.exception("bot shutdown failed")
    _application = None
