"""Render the daily net-worth history as a PNG chart, optionally sent to Telegram.

Pulls rows from db.NetWorthSnapshot, plots SGD totals with matplotlib, saves
to /data/charts/networth-<period>.png. The result can be sent to Telegram
via @Sentinel_claude_testbot_bot (dev) or @YourSentinelBot (production).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, date, timedelta
from pathlib import Path

import httpx
import matplotlib

matplotlib.use("Agg")  # headless backend — no display required
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from . import database as db

logger = logging.getLogger(__name__)

CHARTS_DIR = Path("/data/charts")


def _ensure_dir() -> None:
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)


def build_png(period_days: int = 30) -> dict:
    """Render net-worth history over the trailing `period_days` to PNG.

    Returns: {path, n_points, period_days, start, end, net_change_sgd}.
    """
    _ensure_dir()
    cutoff = (date.today() - timedelta(days=period_days)).isoformat()
    s = db.SessionLocal()
    try:
        rows = (s.query(db.NetWorthSnapshot)
                  .filter(db.NetWorthSnapshot.snapshot_date >= cutoff)
                  .order_by(db.NetWorthSnapshot.snapshot_date.asc())
                  .all())
    finally:
        s.close()

    if not rows:
        return {"path": None, "n_points": 0, "period_days": period_days,
                "error": f"No NetWorthSnapshot rows in last {period_days} days"}

    dates = [datetime.strptime(r.snapshot_date, "%Y-%m-%d") for r in rows]
    nw_sgd = [r.net_worth_sgd for r in rows]
    assets = [r.assets_sgd or 0 for r in rows]
    liab = [-(r.liabilities_sgd or 0) for r in rows]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor("#1c1c1e")
    ax.set_facecolor("#1c1c1e")

    ax.plot(dates, nw_sgd, color="#4cd964", linewidth=2.5, label="Net Worth")
    ax.fill_between(dates, assets, alpha=0.15, color="#4cd964", label="Assets")
    ax.fill_between(dates, liab, alpha=0.15, color="#ff3b30", label="Liabilities")
    ax.axhline(0, color="#8e8e93", linewidth=0.5, linestyle="--")

    ax.set_title(f"Sentinel Finance — Net Worth (last {period_days} days)",
                 color="#f0f0f0", fontsize=13, pad=14)
    ax.set_xlabel("")
    ax.set_ylabel("SGD", color="#8e8e93", fontsize=10)
    ax.tick_params(colors="#8e8e93", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#3a3a3c")
    ax.grid(True, linestyle=":", alpha=0.2, color="#8e8e93")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m"))
    ax.legend(loc="upper left", facecolor="#2c2c2e", edgecolor="#3a3a3c",
              labelcolor="#f0f0f0", fontsize=9)

    fig.tight_layout()
    out_path = CHARTS_DIR / f"networth-{period_days}d-{date.today().isoformat()}.png"
    fig.savefig(out_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)

    return {
        "path": str(out_path),
        "n_points": len(rows),
        "period_days": period_days,
        "start": rows[0].snapshot_date,
        "end": rows[-1].snapshot_date,
        "first_nw_sgd": rows[0].net_worth_sgd,
        "last_nw_sgd": rows[-1].net_worth_sgd,
        "net_change_sgd": round(rows[-1].net_worth_sgd - rows[0].net_worth_sgd, 2),
    }


async def send_to_telegram(png_path: str, caption: str, channel: str = "testbot") -> dict:
    """Send the PNG to Telegram via sendPhoto. channel: 'testbot' | 'production'."""
    if channel == "production":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    else:
        token = os.environ.get("TESTBOT_TOKEN", "")
    chat_id = os.environ.get("OWNER_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
    if not token:
        return {"ok": False, "error": f"{channel} token missing"}
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            with open(png_path, "rb") as f:
                r = await c.post(url,
                                 data={"chat_id": chat_id, "caption": caption[:1024]},
                                 files={"photo": ("chart.png", f, "image/png")})
        return {"ok": r.status_code == 200, "status": r.status_code,
                "body": r.text[:200]}
    except Exception as e:
        logger.exception("sendPhoto failed")
        return {"ok": False, "error": str(e)[:200]}
