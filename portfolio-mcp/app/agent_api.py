"""Service-token-gated read-only API for Sentinel AI (the live LLM agent).

Sentinel AI (@YourSentinelBot, OpenClaw + LM Studio + MetaMCP) cannot hold a
Telegram-Login session cookie. To let it answer "what's my net worth?" or run
UAT against Sentinel Finance, it authenticates with a bearer token loaded
from Windows Credential Manager via .env.local.

Token env var: SENTINEL_FINANCE_AGENT_TOKEN (32 url-safe bytes).

Endpoints are READ-ONLY by design. Any future mutation endpoint must be
explicitly added — there is no PATCH/POST/DELETE handler here. Bumping the
agent surface to v3.0 (Mini App AI Copilot) is when transactional endpoints
get added behind a separate human-approval workflow.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, is_dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import balance_sheet as bs
from . import cash_forecast as cf_mod
from . import category_drill
from . import classifier
from . import home as home_mod
from . import income_statement as is_mod

logger = logging.getLogger(__name__)

_TOKEN = os.environ.get("SENTINEL_FINANCE_AGENT_TOKEN", "")


def _version() -> str:
    from pathlib import Path
    for candidate in (Path("/app/VERSION"), Path(__file__).resolve().parent.parent / "VERSION"):
        try:
            return candidate.read_text().strip()
        except Exception:
            continue
    return "unknown"


def _require_agent(req: Request) -> JSONResponse | None:
    """Return None if authorized, else a 401/403 JSONResponse."""
    if not _TOKEN:
        return JSONResponse(
            {"error": "agent_api_disabled",
             "detail": "SENTINEL_FINANCE_AGENT_TOKEN not set on server"},
            status_code=503,
        )
    header = req.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return JSONResponse(
            {"error": "missing_bearer",
             "detail": "Authorization: Bearer <token> required"},
            status_code=401,
        )
    presented = header.split(None, 1)[1].strip()
    # Constant-time-ish compare
    import hmac
    if not hmac.compare_digest(presented, _TOKEN):
        return JSONResponse({"error": "invalid_token"}, status_code=403)
    return None


def _ok(payload: dict) -> JSONResponse:
    return JSONResponse(payload)


# ── Endpoints ────────────────────────────────────────────────────────────────


async def agent_health(req: Request):
    deny = _require_agent(req)
    if deny:
        return deny
    return _ok({
        "ok": True,
        "service": "portfolio-mcp",
        "version": _version(),
        "agent_endpoints": [
            "/api/agent/health",
            "/api/agent/balance_sheet",
            "/api/agent/income_statement?year=<YYYY>",
            "/api/agent/pending_count",
            "/api/agent/cash_forecast?horizon=<days>",
            "/api/agent/classifier/lookup?description=<text>",
            "/api/agent/glance",
        ],
    })


async def agent_balance_sheet(req: Request):
    deny = _require_agent(req)
    if deny:
        return deny
    try:
        data = await bs.build_balance_sheet()
        return _ok(data)
    except Exception as e:
        logger.exception("agent_balance_sheet failed")
        return JSONResponse({"error": "build_failed", "detail": str(e)}, status_code=500)


async def agent_income_statement(req: Request):
    deny = _require_agent(req)
    if deny:
        return deny
    year_param = req.query_params.get("year", "")
    year = int(year_param) if year_param.isdigit() else None
    try:
        data = await is_mod.build_income_statement(year)
        return _ok(data)
    except Exception as e:
        logger.exception("agent_income_statement failed")
        return JSONResponse({"error": "build_failed", "detail": str(e)}, status_code=500)


async def agent_pending_count(req: Request):
    deny = _require_agent(req)
    if deny:
        return deny
    days_param = req.query_params.get("days", "")
    days = int(days_param) if days_param.isdigit() else 60
    try:
        data = await category_drill.pending_reconciliation_count(days=days)
        return _ok(data)
    except Exception as e:
        logger.exception("agent_pending_count failed")
        return JSONResponse({"error": "build_failed", "detail": str(e)}, status_code=500)


async def agent_cash_forecast(req: Request):
    deny = _require_agent(req)
    if deny:
        return deny
    horizon_param = req.query_params.get("horizon", "")
    horizon = int(horizon_param) if horizon_param.isdigit() else 90
    try:
        data = await cf_mod.build_forecast(horizon_days=horizon)
        return _ok(data)
    except Exception as e:
        logger.exception("agent_cash_forecast failed")
        return JSONResponse({"error": "build_failed", "detail": str(e)}, status_code=500)


async def agent_classifier_lookup(req: Request):
    deny = _require_agent(req)
    if deny:
        return deny
    description = req.query_params.get("description", "")
    if not description:
        return JSONResponse(
            {"error": "missing_param", "detail": "?description=<text> required"},
            status_code=400,
        )
    m = classifier.lookup(description)
    if m is None:
        return _ok({"description": description, "match": None})
    return _ok({
        "description": description,
        "match": asdict(m) if is_dataclass(m) else m.__dict__,
    })


async def agent_glance(req: Request):
    deny = _require_agent(req)
    if deny:
        return deny
    try:
        data = await home_mod.build_home_summary()
        return _ok(data)
    except Exception as e:
        logger.exception("agent_glance failed")
        return JSONResponse({"error": "build_failed", "detail": str(e)}, status_code=500)
