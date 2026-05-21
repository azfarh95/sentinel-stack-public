"""IAS 1 balance sheet builder + HTML renderer (v2 — nested tree).

Reads /finance/balance_sheet_config.yaml + liabilities-registry.yaml, pulls
account balances from Firefly III, pulls crypto positions via portfolio_snapshot,
walks a tree of categories per the config, returns a structured dict. HTML
renderer produces a Telegram Mini App-friendly page.
"""
import os
import logging
from datetime import datetime, timezone, timedelta

import httpx
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = "/finance/balance_sheet_config.yaml"
LIABILITIES_PATH = "/finance/liabilities-registry.yaml"

FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


_FF_ACCT_CACHE: dict = {}  # {account_type: {"at": ts, "data": [...]}}
FF_ACCT_TTL = 60


async def _firefly_accounts(account_type: str) -> list:
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat:
        return []
    import time
    now = time.time()
    cached = _FF_ACCT_CACHE.get(account_type)
    if cached and (now - cached["at"]) < FF_ACCT_TTL:
        return cached["data"]
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{FIREFLY_URL}/api/v1/accounts?type={account_type}&limit=200", headers=h)
        data = r.json().get("data", [])
    _FF_ACCT_CACHE[account_type] = {"at": now, "data": data}
    return data


def _balance_sgd(acct: dict, usd_to_sgd: float) -> float:
    bal = float(acct["attributes"]["current_balance"])
    cur = acct["attributes"]["currency_code"]
    if cur == "SGD": return bal
    if cur == "USD": return bal * usd_to_sgd
    return bal


# TTL cache for the Moralis snapshot — used by balance sheet + home + drill-downs.
# Refreshes every CACHE_TTL_SECONDS. Background scheduler keeps it warm via the
# hourly `_refresh_manual_prices_job` indirectly (it doesn't actually populate
# this cache, but the next request after the TTL expires picks up fresh prices).
_SNAP_CACHE: dict = {"at": 0.0, "positions": None, "manual": None}
CACHE_TTL_SECONDS = 90


async def _snapshot_for_classification():
    import time
    now = time.time()
    if _SNAP_CACHE["positions"] is not None and (now - _SNAP_CACHE["at"]) < CACHE_TTL_SECONDS:
        return _SNAP_CACHE["positions"], _SNAP_CACHE["manual"]
    from . import main as portfolio_main
    snap = await portfolio_main.portfolio_snapshot(address=None, save=False)
    positions = snap.get("positions", [])
    manual = snap.get("manual_positions", [])
    _SNAP_CACHE["at"] = now
    _SNAP_CACHE["positions"] = positions
    _SNAP_CACHE["manual"] = manual
    return positions, manual


def invalidate_snapshot_cache():
    """Call after manual position changes (e.g. portfolio_set_manual_auto) so
    next balance-sheet request gets fresh data."""
    _SNAP_CACHE["at"] = 0.0


# ── Tree walker ───────────────────────────────────────────────────────────────

class _Context:
    """Shared state passed through the recursion: pre-fetched data + config."""
    def __init__(self, by_acct_id, positions, manual, usd_to_sgd):
        self.by_acct_id = by_acct_id
        self.positions = positions
        self.manual = manual
        self.usd_to_sgd = usd_to_sgd


def _resolve_leaf(node: dict, ctx: _Context) -> tuple[float, float, list]:
    """Return (total_usd, total_sgd, items) for a leaf node based on its source."""
    items = []
    total_usd = 0.0
    total_sgd = 0.0
    fx = ctx.usd_to_sgd

    src = node.get("source")

    if node.get("firefly_account_ids"):
        for aid in node["firefly_account_ids"]:
            a = ctx.by_acct_id.get(int(aid))
            if not a: continue
            sgd = _balance_sgd(a, fx)
            usd = sgd / fx if fx else 0
            total_sgd += sgd
            total_usd += usd
            items.append({
                "label": a["attributes"]["name"],
                "usd": round(usd, 2), "sgd": round(sgd, 2),
                "currency": a["attributes"]["currency_code"],
                "raw_balance": float(a["attributes"]["current_balance"]),
            })

    elif src == "portfolio_mcp_liquid":
        # Legacy "all liquid tokens" leaf — kept for backwards compat.
        for p in ctx.positions:
            usd = p["usd_value"]
            sgd = usd * fx
            total_usd += usd; total_sgd += sgd
            items.append({"label": f"{p['symbol']} ({p['chain']})",
                          "usd": round(usd, 2), "sgd": round(sgd, 2)})

    elif src == "portfolio_mcp_liquid_chain":
        chain = node.get("chain", "").lower()
        for p in ctx.positions:
            if (p["chain"] or "").lower() != chain: continue
            usd = p["usd_value"]; sgd = usd * fx
            total_usd += usd; total_sgd += sgd
            items.append({"label": p["symbol"],
                          "usd": round(usd, 2), "sgd": round(sgd, 2)})

    elif src == "portfolio_mcp_liquid_other":
        named = {c.lower() for c in (node.get("named_chains") or [])}
        threshold = float(node.get("threshold_usd", 50))
        # Aggregate per-chain to decide eligibility, then sum positions
        chain_totals = _chain_totals(ctx.positions)
        eligible = {c for c, t in chain_totals.items()
                    if c not in named and t >= threshold}
        for p in ctx.positions:
            if (p["chain"] or "").lower() not in eligible: continue
            usd = p["usd_value"]; sgd = usd * fx
            total_usd += usd; total_sgd += sgd
            items.append({"label": f"{p['symbol']} ({p['chain']})",
                          "usd": round(usd, 2), "sgd": round(sgd, 2)})

    elif src == "portfolio_mcp_liquid_dust":
        named = {c.lower() for c in (node.get("named_chains") or [])}
        threshold = float(node.get("threshold_usd", 50))
        chain_totals = _chain_totals(ctx.positions)
        dust_chains = {c for c, t in chain_totals.items()
                       if c not in named and t < threshold}
        for p in ctx.positions:
            if (p["chain"] or "").lower() not in dust_chains: continue
            usd = p["usd_value"]; sgd = usd * fx
            total_usd += usd; total_sgd += sgd
            items.append({"label": f"{p['symbol']} ({p['chain']})",
                          "usd": round(usd, 2), "sgd": round(sgd, 2)})

    elif src == "portfolio_mcp_manual":
        protos = set(node.get("include_protocols") or [])
        for m in ctx.manual:
            proto = m.get("protocol") or ""
            if proto not in protos: continue
            usd = m["usd_value"]; sgd = usd * fx
            total_usd += usd; total_sgd += sgd
            items.append({"label": m["label"], "protocol": proto,
                          "usd": round(usd, 2), "sgd": round(sgd, 2)})

    elif src == "todo":
        # Placeholder leaf — no data yet
        pass

    return round(total_usd, 2), round(total_sgd, 2), items


def _chain_totals(positions: list) -> dict:
    out = {}
    for p in positions:
        c = (p["chain"] or "").lower()
        out[c] = out.get(c, 0.0) + p["usd_value"]
    return out


def _walk_asset_node(node: dict, ctx: _Context) -> dict:
    """Recursively compute a node tree. Returns:
    { id, label, total (sgd, legacy), usd, sgd, children?: [...], items?: [...] }
    """
    out = {"id": node["id"], "label": node["label"]}
    is_todo = node.get("source") == "todo"
    if is_todo:
        out["todo"] = True
    if node.get("children"):
        children = [_walk_asset_node(c, ctx) for c in node["children"]]
        out["children"] = children
        usd = sum(c["usd"] for c in children)
        sgd = sum(c["sgd"] for c in children)
        # Mark parent as TODO if all children are TODO
        if children and all(c.get("todo") for c in children):
            out["todo"] = True
    else:
        usd, sgd, items = _resolve_leaf(node, ctx)
        out["items"] = items
    out["usd"] = round(usd, 2)
    out["sgd"] = round(sgd, 2)
    out["total"] = out["sgd"]   # legacy field
    return out


def _liability_bucket(registry: dict, months_start: int, months_end: int,
                      fx: float) -> tuple[float, float, list]:
    """Sum monthly × months-in-window across all plans. Returns (total_usd, total_sgd, breakdown)."""
    total_sgd = 0.0
    breakdown = []
    for acct in registry.get("accounts", []):
        acct_total = 0.0
        for plan in acct.get("plans", []):
            monthly = float(plan.get("monthly", 0))
            remaining = int(plan.get("remaining_months", 0))
            lo = months_start
            hi = min(remaining, months_end)
            n = max(hi - lo + 1, 0)
            acct_total += monthly * n
        if acct_total > 0:
            breakdown.append({
                "name": acct["name"].split("(")[0].strip(),
                "sgd": round(acct_total, 2),
                "usd": round(acct_total / fx, 2) if fx else 0,
            })
            total_sgd += acct_total
    breakdown.sort(key=lambda b: b["name"].lower())
    total_usd = total_sgd / fx if fx else 0
    return round(total_usd, 2), round(total_sgd, 2), breakdown


async def build_balance_sheet() -> dict:
    config = _load_yaml(CONFIG_PATH)
    usd_to_sgd = float(config.get("usd_to_sgd", 1.34))

    assets = await _firefly_accounts("asset")
    by_acct_id = {int(a["id"]): a for a in assets}
    positions, manual = await _snapshot_for_classification()

    ctx = _Context(by_acct_id, positions, manual, usd_to_sgd)

    current_assets = [_walk_asset_node(n, ctx) for n in config["assets"]["current"]]
    non_current_assets = [_walk_asset_node(n, ctx) for n in config["assets"]["non_current"]]
    total_current_usd = round(sum(n["usd"] for n in current_assets), 2)
    total_current_sgd = round(sum(n["sgd"] for n in current_assets), 2)
    total_nc_usd = round(sum(n["usd"] for n in non_current_assets), 2)
    total_nc_sgd = round(sum(n["sgd"] for n in non_current_assets), 2)
    total_assets_usd = round(total_current_usd + total_nc_usd, 2)
    total_assets_sgd = round(total_current_sgd + total_nc_sgd, 2)

    try:
        registry = _load_yaml(LIABILITIES_PATH)
    except FileNotFoundError:
        registry = {"accounts": []}

    def build_liab_buckets(bucket_defs):
        out = []
        for b in bucket_defs:
            u, s, br = _liability_bucket(registry, b["months_start"], b["months_end"], usd_to_sgd)
            out.append({"id": b["id"], "label": b["label"],
                        "usd": u, "sgd": s, "total": s, "breakdown": br})
        return out

    current_liabs = build_liab_buckets(config["liabilities"]["current"])
    non_current_liabs = build_liab_buckets(config["liabilities"]["non_current"])
    cl_usd = round(sum(b["usd"] for b in current_liabs), 2)
    cl_sgd = round(sum(b["sgd"] for b in current_liabs), 2)
    ncl_usd = round(sum(b["usd"] for b in non_current_liabs), 2)
    ncl_sgd = round(sum(b["sgd"] for b in non_current_liabs), 2)
    total_liab_usd = round(cl_usd + ncl_usd, 2)
    total_liab_sgd = round(cl_sgd + ncl_sgd, 2)
    net_worth_usd = round(total_assets_usd - total_liab_usd, 2)
    net_worth_sgd = round(total_assets_sgd - total_liab_sgd, 2)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_currency": "SGD",
        "usd_to_sgd": usd_to_sgd,
        "assets": {
            "current": {"usd": total_current_usd, "sgd": total_current_sgd,
                        "total": total_current_sgd, "nodes": current_assets},
            "non_current": {"usd": total_nc_usd, "sgd": total_nc_sgd,
                            "total": total_nc_sgd, "nodes": non_current_assets},
            "usd": total_assets_usd, "sgd": total_assets_sgd, "total": total_assets_sgd,
        },
        "liabilities": {
            "current": {"usd": cl_usd, "sgd": cl_sgd, "total": cl_sgd, "buckets": current_liabs},
            "non_current": {"usd": ncl_usd, "sgd": ncl_sgd, "total": ncl_sgd, "buckets": non_current_liabs},
            "usd": total_liab_usd, "sgd": total_liab_sgd, "total": total_liab_sgd,
        },
        "net_worth": net_worth_sgd,
        "net_worth_usd": net_worth_usd,
        "net_worth_sgd": net_worth_sgd,
    }


# ── HTML rendering ────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Balance Sheet — Sentinel Finance</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#1c1c1e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Sentinel">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<link rel="icon" href="/static/icon-192.png">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
if ('serviceWorker' in navigator) {{
  window.addEventListener('load', () => {{
    navigator.serviceWorker.register('/sw.js', {{ scope: '/' }}).catch(()=>{{}});
  }});
}}
</script>
<style>
:root {{
  --bg: var(--tg-theme-bg-color, #1c1c1e);
  --fg: var(--tg-theme-text-color, #f0f0f0);
  --muted: var(--tg-theme-hint-color, #8e8e93);
  --accent: var(--tg-theme-link-color, #4cd964);
  --sep: rgba(255,255,255,0.10);
  --pos: #4cd964;
  --neg: #ff3b30;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 16px 14px 64px;
  background: var(--bg); color: var(--fg);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}}
h1 {{ font-size: 18px; margin: 0 0 4px; }}
.meta {{ color: var(--muted); font-size: 11px; margin-bottom: 16px; }}
.section {{ margin-bottom: 18px; }}
.section h2 {{
  font-size: 13px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--muted); margin: 0 0 8px; font-weight: 600;
}}
/* Hierarchical rows */
.row {{
  display: grid; grid-template-columns: 1fr 92px 92px;
  gap: 8px; align-items: baseline; padding: 4px 0;
}}
.row .label {{ overflow: hidden; text-overflow: ellipsis; }}
.row .amt {{ font-variant-numeric: tabular-nums; text-align: right; }}
.row .amt-usd {{ color: var(--muted); font-size: 0.92em; }}
.lvl-1 {{ font-weight: 700; font-size: 14px; padding: 6px 0 4px; border-top: 1px solid var(--sep); }}
.lvl-2 {{ font-weight: 600; font-size: 13px; }}
.lvl-2 .label {{ padding-left: 12px; }}
.lvl-3 {{ font-weight: 500; font-size: 12px; color: var(--fg); }}
.lvl-3 .label {{ padding-left: 22px; }}
.lvl-item {{ font-size: 11px; color: var(--muted); padding-top: 1px; padding-bottom: 1px; }}
.lvl-item .label {{ padding-left: 32px; }}
.todo {{ opacity: 0.45; font-style: italic; }}
.todo .label::after {{ content: " · TODO"; color: #ffcc00; font-size: 10px; font-style: normal; opacity: 0.7; }}
.colhead {{ display: grid; grid-template-columns: 1fr 92px 92px; gap: 8px;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted);
  padding-bottom: 4px; border-bottom: 1px solid var(--sep); margin-bottom: 4px; }}
.colhead .amt {{ text-align: right; }}

/* Collapsible groups — native <details>/<summary> */
details.grouper {{ margin: 0; }}
details.grouper > summary {{
  display: grid; grid-template-columns: 1fr 92px 92px; gap: 8px;
  align-items: baseline; padding: 4px 0; cursor: pointer;
  list-style: none;
}}
details.grouper > summary::-webkit-details-marker {{ display: none; }}
details.grouper > summary::marker {{ content: ""; }}
details.grouper > summary > .label::before {{ content: "▾  "; color: var(--muted); font-size: 9px; }}
details.grouper:not([open]) > summary > .label::before {{ content: "▸  "; color: var(--muted); font-size: 9px; }}
details.grouper > summary:hover {{ background: rgba(255,255,255,0.03); border-radius: 4px; }}
.subtotal {{
  display: grid; grid-template-columns: 1fr 92px 92px; gap: 8px;
  padding: 6px 0; font-weight: 700; border-top: 1px solid var(--sep); margin-top: 4px;
}}
.subtotal .amt {{ font-variant-numeric: tabular-nums; text-align: right; }}
.grand {{
  display: grid; grid-template-columns: 1fr 92px 92px; gap: 8px;
  padding: 10px 0; border-top: 2px solid var(--accent); border-bottom: 2px solid var(--accent);
  font-size: 15px; font-weight: 700; margin-top: 8px;
}}
.grand .amt {{ font-variant-numeric: tabular-nums; text-align: right; }}
.networth {{
  display: grid; grid-template-columns: 1fr 92px 92px; gap: 8px;
  padding: 14px 0; margin-top: 8px;
  font-size: 17px; font-weight: 700;
}}
.networth.pos {{ color: var(--pos); }}
.networth.neg {{ color: var(--neg); }}
.networth .amt {{ font-variant-numeric: tabular-nums; text-align: right; }}
.footer {{
  margin-top: 18px; color: var(--muted); font-size: 11px; text-align: center;
}}
details summary {{ font-size: 11px; color: var(--muted); cursor: pointer; padding: 3px 0 3px 32px; }}
.breakdown {{ font-size: 11px; color: var(--muted); padding-left: 32px; padding-top: 4px; padding-bottom: 8px; }}
</style>
</head>
<body>
<script>try{{Telegram.WebApp.ready();Telegram.WebApp.expand();}}catch(e){{}}</script>

<div style="margin-bottom:8px;"><a href="/" style="color:var(--accent);font-size:13px;text-decoration:none;">&larr; Home</a></div>
<h1>Balance Sheet</h1>
<div class="meta">As at {generated_local} · base SGD · USD@{usd_to_sgd}</div>

<div class="section">
  <h2>Assets</h2>
  <div class="colhead"><span>&nbsp;</span><span class="amt">USD</span><span class="amt">SGD</span></div>

  <div class="row lvl-1"><span class="label">Current Assets</span><span class="amt amt-usd">${ca_usd:,.2f}</span><span class="amt">${ca_sgd:,.2f}</span></div>
{current_assets_html}

  <div class="row lvl-1" style="margin-top:6px;"><span class="label">Non-Current Assets</span><span class="amt amt-usd">${nca_usd:,.2f}</span><span class="amt">${nca_sgd:,.2f}</span></div>
{non_current_assets_html}

  <div class="grand"><span>Total Assets</span><span class="amt amt-usd">${ta_usd:,.2f}</span><span class="amt">${ta_sgd:,.2f}</span></div>
</div>

<div class="section">
  <h2>Liabilities</h2>
  <div class="colhead"><span>&nbsp;</span><span class="amt">USD</span><span class="amt">SGD</span></div>

  <div class="row lvl-1"><span class="label">Current Liabilities</span><span class="amt amt-usd">${cl_usd:,.2f}</span><span class="amt">${cl_sgd:,.2f}</span></div>
{current_liab_html}

  <div class="row lvl-1" style="margin-top:6px;"><span class="label">Non-Current Liabilities</span><span class="amt amt-usd">${ncl_usd:,.2f}</span><span class="amt">${ncl_sgd:,.2f}</span></div>
{non_current_liab_html}

  <div class="grand"><span>Total Liabilities</span><span class="amt amt-usd">${tl_usd:,.2f}</span><span class="amt">${tl_sgd:,.2f}</span></div>
</div>

<div class="networth {nw_class}">
  <span>Net Worth</span><span class="amt amt-usd">${nw_usd:,.2f}</span><span class="amt">${nw_sgd:,.2f}</span>
</div>

<div class="footer">Sentinel Finance · IAS 1 presentation</div>
</body>
</html>
"""


def _render_asset_node(node: dict, level: int = 2) -> str:
    cls = f"lvl-{level}" if level <= 3 else "lvl-3"
    if node.get("todo"):
        cls += " todo"
    usd = node.get("usd", 0)
    sgd = node.get("sgd", 0)
    row_inner = (
        f'<span class="label">{node["label"]}</span>'
        f'<span class="amt amt-usd">${usd:,.2f}</span>'
        f'<span class="amt">${sgd:,.2f}</span>'
    )

    children = node.get("children") or []
    items = sorted(node.get("items") or [], key=lambda x: -x.get("sgd", 0))

    if children:
        kids_html = "".join(_render_asset_node(c, level + 1) for c in children)
        return (
            f'  <details class="grouper">'
            f'<summary class="row {cls}">{row_inner}</summary>'
            f'  {kids_html}</details>\n'
        )

    # No children — leaf node
    if not items:
        return f'  <div class="row {cls}">{row_inner}</div>\n'

    # Single item that just duplicates parent label → hide it, plain row
    if len(items) == 1 and (items[0].get("label") == node["label"]):
        return f'  <div class="row {cls}">{row_inner}</div>\n'

    # Multiple items: wrap as collapsible group
    cutoff = 5
    head, tail = items[:cutoff], items[cutoff:]
    inner = ""
    for it in head:
        inner += (
            f'  <div class="row lvl-item"><span class="label">{it.get("label","?")}</span>'
            f'<span class="amt amt-usd">${it.get("usd",0):,.2f}</span>'
            f'<span class="amt">${it.get("sgd",0):,.2f}</span></div>\n'
        )
    if tail:
        rest_sgd = sum(i.get("sgd", 0) for i in tail)
        rest_usd = sum(i.get("usd", 0) for i in tail)
        inner += (
            f'  <details class="grouper"><summary class="row lvl-item">'
            f'<span class="label">+{len(tail)} more</span>'
            f'<span class="amt amt-usd">${rest_usd:,.2f}</span>'
            f'<span class="amt">${rest_sgd:,.2f}</span></summary>'
        )
        for it in tail:
            inner += (
                f'<div class="row lvl-item"><span class="label">{it.get("label","?")}</span>'
                f'<span class="amt amt-usd">${it.get("usd",0):,.2f}</span>'
                f'<span class="amt">${it.get("sgd",0):,.2f}</span></div>'
            )
        inner += '</details>\n'

    return (
        f'  <details class="grouper">'
        f'<summary class="row {cls}">{row_inner}</summary>'
        f'  {inner}</details>\n'
    )


def _render_liab_bucket(b: dict) -> str:
    row_inner = (
        f'<span class="label">{b["label"]}</span>'
        f'<span class="amt amt-usd">${b.get("usd",0):,.2f}</span>'
        f'<span class="amt">${b["sgd"]:,.2f}</span>'
    )
    if not b.get("breakdown"):
        return f'  <div class="row lvl-2">{row_inner}</div>\n'

    inner = ""
    for it in b["breakdown"]:
        inner += (
            f'  <div class="row lvl-item"><span class="label">{it["name"]}</span>'
            f'<span class="amt amt-usd">${it.get("usd",0):,.2f}</span>'
            f'<span class="amt">${it["sgd"]:,.2f}</span></div>\n'
        )
    return (
        f'  <details class="grouper"><summary class="row lvl-2">{row_inner}</summary>'
        f'  {inner}</details>\n'
    )


def render_html(data: dict) -> str:
    a = data["assets"]
    l = data["liabilities"]
    nw_sgd = data["net_worth_sgd"]
    nw_usd = data["net_worth_usd"]

    current_html = "".join(_render_asset_node(n, 2) for n in a["current"]["nodes"])
    non_current_html = "".join(_render_asset_node(n, 2) for n in a["non_current"]["nodes"])
    current_liab_html = "".join(_render_liab_bucket(b) for b in l["current"]["buckets"])
    non_current_liab_html = "".join(_render_liab_bucket(b) for b in l["non_current"]["buckets"])

    try:
        gen_utc = datetime.fromisoformat(data["generated_at_utc"].replace("Z", "+00:00"))
        local = gen_utc + timedelta(hours=8)
        gen_local = local.strftime("%d %b %Y %H:%M SGT")
    except Exception:
        gen_local = data["generated_at_utc"]

    return _HTML_TEMPLATE.format(
        generated_local=gen_local,
        usd_to_sgd=data["usd_to_sgd"],
        ca_usd=a["current"]["usd"], ca_sgd=a["current"]["sgd"],
        nca_usd=a["non_current"]["usd"], nca_sgd=a["non_current"]["sgd"],
        ta_usd=a["usd"], ta_sgd=a["sgd"],
        cl_usd=l["current"]["usd"], cl_sgd=l["current"]["sgd"],
        ncl_usd=l["non_current"]["usd"], ncl_sgd=l["non_current"]["sgd"],
        tl_usd=l["usd"], tl_sgd=l["sgd"],
        nw_usd=nw_usd, nw_sgd=nw_sgd,
        current_assets_html=current_html,
        non_current_assets_html=non_current_html,
        current_liab_html=current_liab_html,
        non_current_liab_html=non_current_liab_html,
        nw_class="pos" if nw_sgd >= 0 else "neg",
    )
