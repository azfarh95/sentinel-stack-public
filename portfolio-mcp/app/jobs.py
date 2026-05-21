"""Scheduler jobs — extracted from main.py for testability and isolation.

Each function is a small async wrapper over a domain module. They're
registered via `register_jobs(scheduler)` which `main._lifespan` calls during
container startup.

Design principles:
- Jobs do one thing, log a structured-ish summary, and swallow exceptions
  (the scheduler must not crash on a single failure).
- Jobs reference module-level imports, not closures over local state — so
  they can be unit-tested in isolation.
- Cron / interval triggers + grace periods are declared in register_jobs(),
  not buried in the function body.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import balance_sheet as bs
from . import config
from . import database as db
from . import dexscreener
from . import polling
from . import wise as wise_mod
from . import wolfswap

logger = logging.getLogger(__name__)


# ── Individual jobs ──────────────────────────────────────────────────────────


async def poll_onchain():
    """Every POLL_INTERVAL_MIN minutes: poll the default wallet for new tx and
    send Telegram alerts if any spikes detected."""
    try:
        n = await polling.poll_once(config.DEFAULT_ADDR)
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


async def wise_sync():
    """Daily: pull Wise balances + post a GL anchor journal to 1113.
    Replaces the old Firefly opening_balance write."""
    try:
        result = await wise_mod.sync_now()
        logger.info("wise sync: total SGD %.2f across %d currencies",
                    result["total_sgd"], len(result["currencies"]))
        try:
            bs.invalidate_snapshot_cache()
        except Exception:
            pass
    except Exception:
        logger.exception("wise sync failed")


async def coinbase_snapshot():
    """Every 15 min: call Coinbase CDP API and persist a row in account_snapshot.
    The resolver never calls the API directly — it reads the snapshot table
    (audit-5 #3 / audit-6 Q3 fail-closed SoT)."""
    from . import coinbase as _cb
    s = db.SessionLocal()
    try:
        snap_id = _cb.refresh_snapshot(s)
        if snap_id is not None:
            logger.info("coinbase_snapshot: wrote row id=%s", snap_id)
    except Exception:
        logger.exception("coinbase_snapshot job failed")
    finally:
        s.close()


async def wise_snapshot():
    """Hourly: pull Wise multi-currency balances + persist account_snapshot row.
    Class B SoT writer for Wise (1113). Parallel to coinbase_snapshot — same
    table, different source_type / provider (audit-6 Q3)."""
    s = db.SessionLocal()
    try:
        snap_id = await wise_mod.refresh_snapshot(s)
        if snap_id is not None:
            logger.info("wise_snapshot: wrote row id=%s", snap_id)
    except Exception:
        logger.exception("wise_snapshot job failed")
    finally:
        s.close()


async def alerts_scan():
    """Daily: run app.alerts.scan(); push NEW high-severity alerts via Telegram.
    Per Perplexity pass-7 Q3 — alerts are read-many, write-once, off the
    hot write path. Gates 1-5 stay deterministic."""
    from . import alerts as _al
    from . import notifier
    from sqlalchemy import text as _text
    s = db.SessionLocal()
    try:
        out = _al.scan(s)
        # Push newly-inserted high-severity alerts
        if out["new_ids"]:
            high = s.execute(_text("""
              SELECT id, message, severity FROM alerts
              WHERE id IN ({}) AND severity='high' AND notified_at IS NULL
            """.format(",".join(str(i) for i in out["new_ids"])))).fetchall()
            for aid, msg, sev in high:
                pushed = await notifier.send(f"⚠ <b>{sev.upper()}</b>: {msg}")
                if pushed:
                    s.execute(_text(
                        "UPDATE alerts SET notified_at=CURRENT_TIMESTAMP WHERE id=:i"
                    ), {"i": aid})
            s.commit()
        logger.info("alerts_scan: new=%d updated=%d", out["new"], out["updated"])
    except Exception:
        logger.exception("alerts_scan job failed")
    finally:
        s.close()


async def drift_nudge():
    """Monthly: nudge owner if any untriaged PERIOD_DRIFT items remain.
    Per Perplexity pass-7 Q1 (c) — the drift queue should never silently
    grow. Telegram push reminds the user to spend 5 min triaging."""
    from datetime import datetime, timezone
    from sqlalchemy import text as _text
    from . import notifier
    s = db.SessionLocal()
    try:
        row = s.execute(_text("""
          SELECT COUNT(*) AS n, MIN(created_at) AS oldest
          FROM unreconciled_queue
          WHERE tx_type='PERIOD_DRIFT' AND status='pending'
        """)).fetchone()
        n = int(row[0] or 0)
        oldest = row[1]
        if n == 0:
            logger.info("drift_nudge: queue is empty — no push")
            return
        # Age of oldest in days
        if oldest:
            if isinstance(oldest, str):
                oldest_dt = datetime.fromisoformat(oldest.replace("Z","").split(".")[0])
            else:
                oldest_dt = oldest
            if oldest_dt.tzinfo is None:
                oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - oldest_dt).days
        else:
            age_days = 0
        msg = (f"📊 <b>Drift queue nudge</b>\n"
               f"{n} period-drift item(s) pending triage.\n"
               f"Oldest is {age_days}d old.\n"
               f"Review: <a href='https://sentinelfinance.your-domain.example.com/reconcile'>/reconcile</a>")
        await notifier.send(msg)
        logger.info("drift_nudge: pushed (n=%d, oldest=%dd)", n, age_days)
    except Exception:
        logger.exception("drift_nudge job failed")
    finally:
        s.close()


async def refresh_manual_prices():
    """Hourly: refresh auto-priced manual positions.
    1. WolfSwap rows → re-read on-chain staked amount
    2. All rows → DexScreener price
    3. Recompute usd_value = amount × price
    """
    s = db.SessionLocal()
    try:
        rows = (s.query(db.ManualPosition)
                  .filter(db.ManualPosition.token_address.isnot(None),
                          db.ManualPosition.token_amount.isnot(None)).all())
        updated = 0
        for r in rows:
            try:
                if (r.protocol or "").lower() == "wolfswap":
                    await _refresh_wolfswap_amount(r, config.DEFAULT_ADDR)
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


async def daily_backup():
    """Tar finance/*.yaml + Firefly REST export to /data/backups."""
    try:
        from . import backup as _backup
        m = await _backup.run_backup()
        logger.info("daily backup ok: %s (%d KB, pruned %d)",
                    m["archive_path"], m["size_kb"], len(m.get("pruned", [])))
    except Exception:
        logger.exception("daily backup failed")


async def onedrive_watcher():
    """Hourly: scan OneDrive Auto-import folder, post new CSVs to Firefly,
    ping testbot if new imports."""
    try:
        from . import posb_ibanking_importer as _imp
        from . import notifier as _notifier
        r = _imp.scan_and_import(move_after=True, triggered_by="hourly_watcher")
        created_total = sum((x.get("created", 0) or 0) for x in r.get("results", []))
        if created_total > 0:
            lines = [f"Auto-import found {created_total} new tx across "
                     f"{r['scanned']} file(s):"]
            for x in r["results"]:
                if x.get("created", 0) > 0:
                    v = x.get("variance")
                    vtext = f" · variance {v:+.2f}" if v is not None else ""
                    lines.append(f"  • {x['file']} → {x['account_name']}: "
                                 f"{x['created']} created · {x['dup']} dup{vtext}")
            try:
                await _notifier.send_to_testbot("\n".join(lines))
            except Exception:
                logger.exception("watcher testbot ping failed")
    except Exception:
        logger.exception("OneDrive watcher job failed")


async def networth_snapshot():
    """Daily 02:30 — capture net worth row into history table. Idempotent per date."""
    try:
        from . import networth_history as _nw
        r = await _nw.capture_today()
        logger.info("networth snapshot %s: SGD %.2f", r["mode"], r["net_worth_sgd"])
    except Exception:
        logger.exception("networth snapshot failed")


async def morningstar_nav():
    """Daily 06:00 — refresh Morningstar SG NAV for tracked funds."""
    try:
        from . import morningstar_sg as _ms
        r = await _ms.refresh_all()
        logger.info("morningstar refresh: %d scanned, %d updated, %d skipped",
                    r["scanned"], r["updated"], r["skipped"])
    except Exception:
        logger.exception("morningstar refresh failed")


async def firefly_bridge():
    """Daily 07:00 — bridge last 7 days of Firefly tx into GL. Idempotent on
    external_id."""
    try:
        from . import firefly_bridge as _fb
        from datetime import date as _date, timedelta as _td
        start = (_date.today() - _td(days=7)).isoformat()
        end = _date.today().isoformat()
        stats = await _fb.bridge(start=start, end=end)
        logger.info("firefly bridge %s→%s: %s", start, end, stats)
    except Exception:
        logger.exception("firefly bridge job failed")


# ── Registration ─────────────────────────────────────────────────────────────


def register_jobs(scheduler: AsyncIOScheduler) -> list[str]:
    """Wire all jobs onto the given scheduler. Returns list of registered job IDs.

    Does NOT start the scheduler — the lifespan handler calls scheduler.start()
    after this returns. Does NOT run startup catch-ups — those are explicit
    awaits in the lifespan handler after registration.
    """
    registered = []

    # Always-on jobs (require default wallet to be configured)
    if config.DEFAULT_ADDR:
        scheduler.add_job(poll_onchain, "interval", minutes=config.POLL_INTERVAL_MIN,
                          id="onchain_poll", replace_existing=True)
        registered.append("onchain_poll")

        scheduler.add_job(refresh_manual_prices, "interval", minutes=60,
                          id="manual_price_refresh", replace_existing=True)
        registered.append("manual_price_refresh")

    # Wise — only if token is configured
    if config.WISE_API_TOKEN:
        scheduler.add_job(wise_sync, CronTrigger(hour=6, minute=30),
                          id="wise_sync", replace_existing=True,
                          misfire_grace_time=3600, coalesce=True)
        registered.append("wise_sync")

    scheduler.add_job(daily_backup, CronTrigger(hour=2, minute=0),
                      id="daily_backup", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True)
    registered.append("daily_backup")

    scheduler.add_job(onedrive_watcher, "interval", minutes=60,
                      id="onedrive_watcher", replace_existing=True)
    registered.append("onedrive_watcher")

    scheduler.add_job(networth_snapshot, CronTrigger(hour=2, minute=30),
                      id="nw_snapshot", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True)
    registered.append("nw_snapshot")

    scheduler.add_job(morningstar_nav, CronTrigger(hour=6, minute=0),
                      id="morningstar_nav", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True)
    registered.append("morningstar_nav")

    scheduler.add_job(firefly_bridge, CronTrigger(hour=7, minute=0),
                      id="firefly_bridge", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True)
    registered.append("firefly_bridge")

    # Coinbase — Class B SoT writer. Only if the CDP key file is present.
    from pathlib import Path as _Path
    if _Path("/data/coinbase_cdp_key.json").exists():
        scheduler.add_job(coinbase_snapshot, "interval", minutes=15,
                          id="coinbase_snapshot", replace_existing=True,
                          misfire_grace_time=900, coalesce=True)
        registered.append("coinbase_snapshot")

    # Wise — Class B SoT writer (audit-6 Q3). Hourly is enough for Wise.
    if config.WISE_API_TOKEN:
        scheduler.add_job(wise_snapshot, "interval", minutes=60,
                          id="wise_snapshot", replace_existing=True,
                          misfire_grace_time=900, coalesce=True)
        registered.append("wise_snapshot")

    # Drift nudge — monthly, 1st @ 09:00 SGT (01:00 UTC). Per pass-7 Q1.
    scheduler.add_job(drift_nudge, CronTrigger(day=1, hour=1, minute=0),
                      id="drift_nudge", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True)
    registered.append("drift_nudge")

    # Alerts scan — daily @ 03:00 SGT (19:00 UTC prior day). Per pass-7 Q3.
    scheduler.add_job(alerts_scan, CronTrigger(hour=19, minute=0),
                      id="alerts_scan", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True)
    registered.append("alerts_scan")

    return registered
