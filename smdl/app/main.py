"""SM-DL service — FastAPI health endpoint + Telegram bot lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import database as db
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
    asyncio.create_task(start_cleanup_loop())

    tg_app = await build()
    await tg_app.initialize()
    await tg_app.start()

    polling_task = asyncio.create_task(
        tg_app.updater.start_polling(drop_pending_updates=True)
    )

    def _on_task_done(t: asyncio.Task):
        if not t.cancelled() and t.exception():
            logger.error("Polling task crashed: %s", t.exception(), exc_info=t.exception())

    polling_task.add_done_callback(_on_task_done)
    logger.info("SM-DL bot polling started")
    yield
    polling_task.cancel()
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    logger.info("SM-DL bot shut down")


app = FastAPI(title="SM-DL — Social Media Downloader", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sm-dl"}
