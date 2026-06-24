"""SM-DL service — FastAPI health endpoint + Telegram bot lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import database as db
from . import file_serve
from . import interceptor  # noqa: F401 — triggers plugin auto-load at startup
from . import miniapp
from . import stream_monitor
from .bot import build
from .downloader import start_cleanup_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    # First-boot: default-block adult cam platforms so they don't appear in
    # non-owner UX. Owner can flip them back on in Admin → Sites.
    try:
        from . import auth as _auth
        if await _auth.seed_default_blocklist_if_unset():
            logger.info("Seeded default site blocklist: %s",
                        _auth.DEFAULT_BLOCKED_PLATFORMS)
    except Exception as _e:
        logger.warning("seed_default_blocklist_if_unset failed: %s", _e)

    # Self-heal: an earlier release (pre-fix) inserted the owner's row as
    # 'pending'. Flip any owner row back to active so it stops showing in
    # the Admin → Pending list. Also drop any pending row whose chat_id is
    # negative — those are Telegram group chats wrongly recorded by an
    # earlier bug.
    try:
        from .config import OWNER_CHAT_ID
        import aiosqlite as _aio
        async with _aio.connect(db.DB_PATH) as conn:
            if OWNER_CHAT_ID is not None:
                cur = await conn.execute(
                    "UPDATE users SET status='active', pending_code=NULL, "
                    "pending_expires_at=NULL "
                    "WHERE chat_id = ? AND status = 'pending'",
                    (int(OWNER_CHAT_ID),),
                )
                if cur.rowcount:
                    logger.info("Healed owner row: flipped %d pending row(s) to active", cur.rowcount)
            cur = await conn.execute("DELETE FROM users WHERE chat_id < 0")
            if cur.rowcount:
                logger.info("Cleaned up %d group-chat user rows (chat_id < 0)", cur.rowcount)
            await conn.commit()
    except Exception as _e:
        logger.warning("user-row self-heal failed: %s", _e)
    asyncio.create_task(start_cleanup_loop())

    # Bot initialization is best-effort. A bad/missing token must NOT crash
    # the FastAPI lifespan — keep the /health endpoint up so the operator
    # can curl it, check container logs, and fix the token without
    # container-crashloop noise. Same logic helps the fresh-install test
    # spin up without a real BotFather token.
    tg_app = None
    polling_task = None
    monitor_task = None
    try:
        tg_app = await build()
        await tg_app.initialize()
        await tg_app.start()
        polling_task = asyncio.create_task(
            tg_app.updater.start_polling(drop_pending_updates=True)
        )
        monitor_task = asyncio.create_task(stream_monitor.monitor_loop(tg_app))

        def _on_task_done(t: asyncio.Task):
            if not t.cancelled() and t.exception():
                logger.error("Background task crashed: %s", t.exception(), exc_info=t.exception())

        polling_task.add_done_callback(_on_task_done)
        monitor_task.add_done_callback(_on_task_done)
        logger.info("SM-DL bot polling started + stream monitor running")
    except Exception as e:
        logger.error(
            "Bot startup failed (%s: %s). FastAPI continues running for "
            "diagnostics — /health endpoint stays up, but Telegram features "
            "are unavailable until the underlying issue is fixed.",
            type(e).__name__, e,
        )

    yield

    if polling_task and not polling_task.done():
        polling_task.cancel()
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    if tg_app is not None:
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception as e:
            logger.warning("bot shutdown raised: %s", e)
    logger.info("SM-DL bot shut down")


app = FastAPI(title="SM-DL — Social Media Downloader", lifespan=lifespan)
app.include_router(file_serve.router)
app.include_router(miniapp.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sm-dl"}
