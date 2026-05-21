"""Cron-style poll of the Moralis wallet history endpoint. Emits Telegram alerts
for new transactions where USD value > MIN_USD. Runs every 5 minutes via APScheduler."""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from . import database as db
from . import moralis
from . import notifier

logger = logging.getLogger(__name__)

MIN_USD = float(os.environ.get("ONCHAIN_ALERT_MIN_USD", "1.0"))

# Block explorer URL templates per chain
EXPLORER = {
    "eth": "https://etherscan.io/tx/{}",
    "bsc": "https://bscscan.com/tx/{}",
    "polygon": "https://polygonscan.com/tx/{}",
    "arbitrum": "https://arbiscan.io/tx/{}",
    "base": "https://basescan.org/tx/{}",
    "avalanche": "https://snowtrace.io/tx/{}",
    "cronos": "https://explorer.cronos.org/tx/{}",
}

# Chain display labels
CHAIN_LABEL = {
    "eth": "ETH", "bsc": "BSC", "polygon": "POL", "arbitrum": "ARB",
    "base": "BASE", "avalanche": "AVAX", "cronos": "CRO",
}


def _short(addr: str) -> str:
    if not addr or len(addr) < 12:
        return addr or "—"
    return f"{addr[:6]}…{addr[-4:]}"


def _format_tx(tx: dict, chain: str, my_addr: str) -> str:
    """Return a 4-6 line HTML-formatted Telegram message for one transaction."""
    h = tx.get("hash", "")
    cat = tx.get("category", "transfer")
    summary = tx.get("summary", "") or cat
    ts = tx.get("block_timestamp", "")
    explorer = EXPLORER.get(chain, "").format(h) if h else ""
    chain_lbl = CHAIN_LABEL.get(chain, chain)

    # Collect USD values from native + ERC20 transfers
    lines = []
    my = my_addr.lower()
    native = tx.get("native_transfers", []) or []
    for n in native:
        usd = float(n.get("value_usd") or 0)
        if usd < MIN_USD:
            continue
        direction = "IN " if n.get("to_address", "").lower() == my else "OUT"
        amt = n.get("value_formatted") or n.get("value", "?")
        sym = n.get("token_symbol", "")
        cp = n.get("from_address" if direction == "IN " else "to_address", "")
        lines.append(f"{direction} {amt} {sym}  ~${usd:,.2f}  ({_short(cp)})")

    erc = tx.get("erc20_transfers", []) or []
    for e in erc:
        usd = float(e.get("value_usd") or 0)
        if usd < MIN_USD:
            continue
        direction = "IN " if e.get("to_address", "").lower() == my else "OUT"
        amt = e.get("value_formatted") or e.get("value", "?")
        sym = e.get("token_symbol", "")
        cp = e.get("from_address" if direction == "IN " else "to_address", "")
        lines.append(f"{direction} {amt} {sym}  ~${usd:,.2f}  ({_short(cp)})")

    if not lines:
        return ""  # all transfers below threshold

    body = (
        f"<b>{chain_lbl} • {summary}</b>\n"
        + "\n".join(f"  <code>{l}</code>" for l in lines)
        + (f"\n  {ts}" if ts else "")
        + (f"\n  <a href=\"{explorer}\">view tx</a>" if explorer else "")
    )
    return body


async def poll_once(address: str) -> int:
    """Poll all configured chains, emit alerts, update bookmarks. Returns alerts sent."""
    if not address:
        logger.warning("polling skipped: no address configured")
        return 0
    addr = address.lower()
    alerts_sent = 0
    s = db.SessionLocal()
    try:
        for chain in moralis.CHAINS:
            try:
                txs = await moralis.wallet_history(addr, chain, limit=25)
            except Exception as e:
                logger.warning("history(%s) failed: %s", chain, e)
                continue

            bookmark = (s.query(db.LastSeenTx)
                          .filter(db.LastSeenTx.address == addr,
                                  db.LastSeenTx.chain == chain).first())
            seen_hash = bookmark.last_tx_hash if bookmark else None

            new_txs = []
            for tx in txs:
                if tx.get("hash") == seen_hash:
                    break
                new_txs.append(tx)

            new_txs.reverse()  # send oldest-first so Telegram thread reads naturally

            for tx in new_txs:
                msg = _format_tx(tx, chain, addr)
                if not msg:
                    continue  # all sub-threshold
                ok = await notifier.send(msg)
                if ok:
                    alerts_sent += 1
                await asyncio.sleep(0.3)  # rate-limit Telegram

            # Update bookmark to the newest tx we saw, even if all were filtered
            if txs:
                if bookmark:
                    bookmark.last_tx_hash = txs[0].get("hash")
                    bookmark.updated_at = db.now_utc()
                else:
                    s.add(db.LastSeenTx(
                        address=addr, chain=chain,
                        last_tx_hash=txs[0].get("hash"),
                        updated_at=db.now_utc(),
                    ))
                s.commit()
        return alerts_sent
    finally:
        s.close()


async def initialize_bookmarks(address: str) -> None:
    """First run: capture current head per chain WITHOUT alerting. Stops the
    cold-start firehose of historical transactions."""
    if not address:
        return
    addr = address.lower()
    s = db.SessionLocal()
    try:
        for chain in moralis.CHAINS:
            bookmark = (s.query(db.LastSeenTx)
                          .filter(db.LastSeenTx.address == addr,
                                  db.LastSeenTx.chain == chain).first())
            if bookmark:
                continue  # already initialized
            try:
                txs = await moralis.wallet_history(addr, chain, limit=1)
            except Exception as e:
                logger.warning("init bookmark %s failed: %s", chain, e)
                continue
            head = txs[0].get("hash") if txs else None
            s.add(db.LastSeenTx(
                address=addr, chain=chain,
                last_tx_hash=head,
                updated_at=db.now_utc(),
            ))
            logger.info("init bookmark %s/%s @ %s", addr, chain, head)
        s.commit()
    finally:
        s.close()
