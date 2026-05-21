"""Drill-down pages for the home dashboard cards.

Routes:
  /drill/bank        — POSB Savings + Cash wallet history + running balance
  /drill/crypto      — All crypto positions (liquid + LP + staking) breakdown
  /drill/loans       — Non-CC liabilities: balance + monthly schedule
  /drill/cc          — Credit cards: balance + monthly schedule
  /drill/recurring   — Monthly recurring expenses (insurance + debt service)
"""
import os
import logging
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict

import httpx
import yaml

from . import balance_sheet as bs

logger = logging.getLogger(__name__)
FIREFLY_URL = os.environ.get("FIREFLY_INTERNAL_URL", "http://host.docker.internal:8180")
LIAB_PATH = "/finance/liabilities-registry.yaml"


async def _firefly(path: str, params: dict | None = None) -> dict | list:
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat: return []
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{FIREFLY_URL}/api/v1/{path}",
                        headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
                        params=params or {})
        try: return r.json()
        except: return {}


async def _all_account_transactions(account_id: int, start: str, end: str) -> list:
    pat = os.environ.get("FIREFLY_PAT", "")
    if not pat: return []
    out = []; page = 1
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            r = await c.get(f"{FIREFLY_URL}/api/v1/accounts/{account_id}/transactions",
                            headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
                            params={"start": start, "end": end, "limit": 200, "page": page})
            data = r.json()
            rows = data.get("data", [])
            out.extend(rows)
            meta = data.get("meta", {}).get("pagination", {})
            if page >= int(meta.get("total_pages", 1) or 1): break
            page += 1
    return out


# ── Builders ─────────────────────────────────────────────────────────────────

async def build_bank_drill(days: int = 60) -> dict:
    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    # POSB + Cash wallet
    txs_posb = await _all_account_transactions(1, start, end)
    txs_cash = await _all_account_transactions(4, start, end)
    all_txs = []
    for t in txs_posb:
        tx = t["attributes"]["transactions"][0]
        sign = 1 if str(tx.get("destination_id")) == "1" else -1
        all_txs.append({
            "date": tx["date"][:10], "amt_signed": sign * float(tx["amount"]),
            "type": tx["type"], "desc": tx.get("description", "")[:60],
            "src": (tx.get("source_name") or "?")[:25],
            "dst": (tx.get("destination_name") or "?")[:25],
            "cat": tx.get("category_name") or "—",
            "account": "POSB",
        })
    for t in txs_cash:
        tx = t["attributes"]["transactions"][0]
        sign = 1 if str(tx.get("destination_id")) == "4" else -1
        all_txs.append({
            "date": tx["date"][:10], "amt_signed": sign * float(tx["amount"]),
            "type": tx["type"], "desc": tx.get("description", "")[:60],
            "src": (tx.get("source_name") or "?")[:25],
            "dst": (tx.get("destination_name") or "?")[:25],
            "cat": tx.get("category_name") or "—",
            "account": "Cash",
        })
    all_txs.sort(key=lambda x: x["date"], reverse=True)

    posb_bal = float((await _firefly("accounts/1"))["data"]["attributes"]["current_balance"])
    cash_bal = float((await _firefly("accounts/4"))["data"]["attributes"]["current_balance"])

    return {
        "title": "Bank Balance",
        "period_days": days,
        "start": start, "end": end,
        "current_balance_sgd": round(posb_bal + cash_bal, 2),
        "breakdown": [
            {"name": "POSB Savings", "sgd": round(posb_bal, 2)},
            {"name": "Cash Wallet", "sgd": round(cash_bal, 2)},
        ],
        "transactions": all_txs[:200],   # cap for sanity
    }


async def build_crypto_drill() -> dict:
    """Live snapshot of all crypto positions across chains + manual."""
    from . import main as portfolio_main
    snap = await portfolio_main.portfolio_snapshot(address=None, save=False)
    positions = sorted(snap.get("positions", []), key=lambda p: -p["usd_value"])
    manual = sorted(snap.get("manual_positions", []), key=lambda m: -m["usd_value"])
    fx = float((yaml.safe_load(open("/finance/balance_sheet_config.yaml"))).get("usd_to_sgd", 1.27))

    # CEX accounts (Firefly Coinbase + Crypto.com)
    cex = []
    for fid, label in [(97, "Coinbase Account"), (98, "Crypto.com Account")]:
        a = (await _firefly(f"accounts/{fid}"))["data"]["attributes"]
        bal = float(a["current_balance"])
        if a.get("currency_code") == "USD":
            cex.append({"name": label, "usd": bal, "sgd": bal * fx})
        else:
            cex.append({"name": label, "usd": bal / fx, "sgd": bal})

    total_usd = sum(p["usd_value"] for p in positions) + sum(m["usd_value"] for m in manual) + sum(c["usd"] for c in cex)

    return {
        "title": "Crypto Holdings",
        "fx": fx,
        "totals": {
            "usd": round(total_usd, 2),
            "sgd": round(total_usd * fx, 2),
        },
        "liquid_positions": [
            {"symbol": p["symbol"], "chain": p["chain"],
             "usd": round(p["usd_value"], 2), "sgd": round(p["usd_value"] * fx, 2)}
            for p in positions
        ],
        "manual_positions": [
            {"label": m["label"], "protocol": m.get("protocol") or "—",
             "chain": m["chain"], "usd": round(m["usd_value"], 2), "sgd": round(m["usd_value"] * fx, 2)}
            for m in manual
        ],
        "cex_accounts": [{"name": c["name"], "usd": round(c["usd"], 2), "sgd": round(c["sgd"], 2)} for c in cex],
    }


async def build_liability_drill(only_type: str | None = None) -> dict:
    """Group by registry account. only_type: 'credit_card' for CC drill, else loans (everything else)."""
    reg = yaml.safe_load(open(LIAB_PATH))
    rows = []
    for acct in reg["accounts"]:
        atype = acct.get("type", "")
        if only_type == "credit_card" and atype != "credit_card": continue
        if only_type == "loans" and atype == "credit_card": continue

        # current outstanding from Firefly (more accurate than registry's snapshot)
        try:
            a = await _firefly(f"accounts/{acct['firefly_acct_id']}")
            current = abs(float(a["data"]["attributes"]["current_balance"]))
        except Exception:
            current = float(acct.get("current_outstanding", 0))

        plans = [
            {"plan_code": p.get("plan_code", p.get("id", "")),
             "monthly": float(p.get("monthly", 0)),
             "remaining_months": int(p.get("remaining_months", 0)),
             "outstanding": float(p.get("outstanding", 0)) if p.get("outstanding") else None,
             "kind": p.get("kind", "")}
            for p in acct.get("plans", [])
        ]
        rows.append({
            "id": acct["id"],
            "ff_id": acct["firefly_acct_id"],
            "name": acct["name"],
            "short_name": acct["name"].split("(")[0].strip(),
            "type": atype,
            "billing_day": acct.get("billing_day"),
            "credit_limit": float(acct.get("credit_limit", 0)) if acct.get("credit_limit") else None,
            "available": float(acct.get("available", 0)) if acct.get("available") else None,
            "outstanding": round(current, 2),
            "monthly_total": round(sum(p["monthly"] for p in plans), 2),
            "plans": plans,
        })
    rows.sort(key=lambda x: -x["outstanding"])
    return {
        "title": "Credit Cards" if only_type == "credit_card" else "Loans",
        "total_outstanding": round(sum(r["outstanding"] for r in rows), 2),
        "total_monthly": round(sum(r["monthly_total"] for r in rows), 2),
        "accounts": rows,
    }


async def build_funds_drill() -> dict:
    """Read finance/funds.yaml + compute policy totals + per-fund values."""
    funds_data = yaml.safe_load(open("/finance/funds.yaml"))
    cfg = yaml.safe_load(open("/finance/balance_sheet_config.yaml"))
    fx = float(cfg.get("usd_to_sgd", 1.27))
    today = date.today()

    funds = []
    for f in funds_data.get("funds", []):
        nav = float(f.get("last_nav") or 0)
        ccy = f.get("currency", "SGD")
        nav_date = f.get("last_nav_date", "")
        try:
            age_days = (today - date.fromisoformat(nav_date)).days
        except Exception:
            age_days = 999
        rows = []
        total_units = 0.0
        total_sgd = 0.0
        for h in f.get("holdings", []):
            units = float(h["units"])
            ccy_value = units * nav
            sgd_value = ccy_value * (fx if ccy == "USD" else 1.0)
            total_units += units
            total_sgd += sgd_value
            rows.append({
                "policy": h["policy"], "units": round(units, 5),
                "value_ccy": round(ccy_value, 2),
                "value_sgd": round(sgd_value, 2),
            })
        funds.append({
            "id": f["id"], "name": f["name"],
            "currency": ccy, "nav": nav, "nav_date": nav_date,
            "age_days": age_days, "stale": age_days > 30,
            "total_units": round(total_units, 5),
            "total_sgd": round(total_sgd, 2),
            "holdings": rows,
        })
    funds.sort(key=lambda x: -x["total_sgd"])

    # Policy summary
    policy_sums = {}
    for f in funds:
        for h in f["holdings"]:
            policy_sums[h["policy"]] = policy_sums.get(h["policy"], 0.0) + h["value_sgd"]
    policies = sorted([{"name": p, "sgd": round(v, 2)} for p, v in policy_sums.items()],
                      key=lambda x: -x["sgd"])

    return {
        "title": "Fund Universe",
        "total_sgd": round(sum(f["total_sgd"] for f in funds), 2),
        "fx_usd_to_sgd": fx,
        "fund_count": len(funds),
        "stale_count": sum(1 for f in funds if f["stale"]),
        "funds": funds,
        "policies": policies,
    }


async def build_recurring_drill() -> dict:
    """Authoritative source: finance/recurring.yaml. Groups expenses by category
    (Insurance, ILP, Debt service, Other) for cleaner display.
    """
    sched = yaml.safe_load(open("/finance/recurring.yaml"))
    expenses = [e for e in sched.get("expense", []) if e.get("enabled", True)]

    # Group by category bucket
    buckets = {
        "ILP (asset transfer)": [],
        "Insurance (expense)": [],
        "Debt service": [],
        "Other": [],
    }
    for e in expenses:
        cat = (e.get("category") or "").lower()
        if "ilp" in cat:
            buckets["ILP (asset transfer)"].append(e)
        elif "insurance" in cat:
            buckets["Insurance (expense)"].append(e)
        elif "debt" in cat:
            buckets["Debt service"].append(e)
        else:
            buckets["Other"].append(e)

    # Income (for net per month)
    incomes = [i for i in sched.get("income", []) if i.get("enabled", True)]
    income_total = sum(float(i["amount"]) for i in incomes)

    bucket_summaries = []
    grand_total = 0.0
    for label, items in buckets.items():
        if not items: continue
        rows = sorted(items, key=lambda x: -float(x["amount"]))
        subtotal = sum(float(r["amount"]) for r in rows)
        grand_total += subtotal
        bucket_summaries.append({
            "label": label,
            "subtotal": round(subtotal, 2),
            "items": [
                {"name": r["name"], "amount": round(float(r["amount"]), 2),
                 "day": r.get("day"), "category": r.get("category", "")}
                for r in rows
            ],
        })

    return {
        "title": "Monthly Recurring",
        "grand_total": round(grand_total, 2),
        "income_total": round(income_total, 2),
        "net_monthly": round(income_total - grand_total, 2),
        "buckets": bucket_summaries,
        "income": [{"name": i["name"], "amount": round(float(i["amount"]), 2),
                    "day": i.get("day")} for i in incomes],
    }


# ── HTML rendering ────────────────────────────────────────────────────────────

_CSS = """
:root { --bg:#1c1c1e; --fg:#f0f0f0; --muted:#8e8e93; --accent:#4cd964; --sep:rgba(255,255,255,0.10); --pos:#4cd964; --neg:#ff3b30; --card:#2c2c2e; }
* { box-sizing: border-box; }
body { margin:0; padding:18px 14px 60px; background:var(--bg); color:var(--fg);
  font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 600px; margin-left: auto; margin-right: auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: var(--muted); font-size: 11px; margin-bottom: 14px; }
.back { display:inline-block; color:var(--accent); font-size:13px; text-decoration:none; margin-bottom:6px; }
.big { font-size:24px;font-weight:700;color:var(--accent); margin: 10px 0; }
.subtotal { color: var(--muted); font-size: 12px; }
.card { background: var(--card); border-radius: 12px; padding: 14px 16px; margin: 10px 0;
  border: 1px solid var(--sep); }
.card-row { display: flex; justify-content: space-between; align-items: baseline; padding: 5px 0;
  font-size: 13px; }
.card-row .name { flex: 1; }
.card-row .amt { font-variant-numeric: tabular-nums; }
.card-row .sub { color: var(--muted); font-size: 11px; }
.section-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--muted); margin: 14px 4px 4px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 4px; }
th, td { padding: 6px 4px; text-align: left; border-bottom: 1px solid var(--sep); }
th { color: var(--muted); font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; }
td.amt { text-align: right; font-variant-numeric: tabular-nums; }
.pos { color: var(--pos); } .neg { color: var(--neg); } .muted { color: var(--muted); }
.tx-list { font-size: 12px; margin-top: 8px; }
.tx-row { display: grid; grid-template-columns: 80px 1fr 80px; gap: 8px;
  padding: 6px 0; border-bottom: 1px solid var(--sep); align-items: baseline; }
.tx-row .d { color: var(--muted); font-variant-numeric: tabular-nums; }
.tx-row .desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tx-row .amt { text-align: right; font-variant-numeric: tabular-nums; }
.tx-row .meta { color: var(--muted); font-size: 10px; }
footer { color:var(--muted); font-size:10px; text-align:center; margin-top:24px; }
"""


def _layout(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f'<title>{title} — Sentinel Finance</title>'
        f'<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#1c1c1e">'
        f'<link rel="apple-touch-icon" href="/static/icon-192.png">'
        f'<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        f'<style>{_CSS}</style>'
        f'<script>try{{Telegram.WebApp.ready();Telegram.WebApp.expand();}}catch(e){{}}</script>'
        f'</head><body>{body}</body></html>'
    )


def render_bank(data: dict) -> str:
    rows_html = ""
    for tx in data["transactions"][:50]:
        sign_cls = "pos" if tx["amt_signed"] > 0 else "neg"
        sign = "+" if tx["amt_signed"] > 0 else "-"
        amt = abs(tx["amt_signed"])
        cat = f' · {tx["cat"]}' if tx["cat"] != "—" else ""
        rows_html += (
            f'<div class="tx-row"><span class="d">{tx["date"][5:]}</span>'
            f'<span class="desc">{tx["desc"]}<div class="meta">{tx["account"]}{cat}</div></span>'
            f'<span class="amt {sign_cls}">{sign}${amt:,.2f}</span></div>'
        )
    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>Bank Balance</h1>'
        f'<div class="big">SGD {data["current_balance_sgd"]:,.2f}</div>'
        '<div class="card">'
        + "".join(f'<div class="card-row"><span class="name">{b["name"]}</span><span class="amt">SGD {b["sgd"]:,.2f}</span></div>'
                  for b in data["breakdown"])
        + '</div>'
        f'<div class="section-label">Recent transactions ({data["period_days"]} days · {len(data["transactions"])} txns)</div>'
        f'<div class="tx-list">{rows_html or "<p class=muted>None</p>"}</div>'
        '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout("Bank Balance", body)


def render_crypto(data: dict) -> str:
    fx = data["fx"]

    def liquid_rows():
        if not data["liquid_positions"]:
            return '<p class="muted" style="padding:8px;font-size:12px;">No liquid positions</p>'
        h = '<table><thead><tr><th>Symbol</th><th>Chain</th><th class="amt">USD</th><th class="amt">SGD</th></tr></thead><tbody>'
        for p in data["liquid_positions"][:30]:
            h += f'<tr><td>{p["symbol"]}</td><td class="muted">{p["chain"]}</td><td class="amt">${p["usd"]:,.2f}</td><td class="amt">${p["sgd"]:,.2f}</td></tr>'
        h += '</tbody></table>'
        return h

    def manual_rows():
        if not data["manual_positions"]:
            return '<p class="muted" style="padding:8px;font-size:12px;">No staking/LP positions</p>'
        h = '<table><thead><tr><th>Position</th><th>Protocol</th><th class="amt">USD</th><th class="amt">SGD</th></tr></thead><tbody>'
        for m in data["manual_positions"]:
            h += f'<tr><td>{m["label"]}</td><td class="muted">{m["protocol"]}</td><td class="amt">${m["usd"]:,.2f}</td><td class="amt">${m["sgd"]:,.2f}</td></tr>'
        h += '</tbody></table>'
        return h

    def cex_rows():
        h = '<table><thead><tr><th>Account</th><th class="amt">USD</th><th class="amt">SGD</th></tr></thead><tbody>'
        for c in data["cex_accounts"]:
            h += f'<tr><td>{c["name"]}</td><td class="amt">${c["usd"]:,.2f}</td><td class="amt">${c["sgd"]:,.2f}</td></tr>'
        h += '</tbody></table>'
        return h

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>Crypto Holdings</h1>'
        f'<div class="big">SGD {data["totals"]["sgd"]:,.2f}</div>'
        f'<div class="subtotal">USD ${data["totals"]["usd"]:,.2f} · FX@{fx}</div>'
        '<div class="section-label">Liquid (Moralis-visible)</div>'
        f'<div class="card" style="padding:8px 12px;">{liquid_rows()}</div>'
        '<div class="section-label">Staking / LP / Vaults</div>'
        f'<div class="card" style="padding:8px 12px;">{manual_rows()}</div>'
        '<div class="section-label">CEX accounts</div>'
        f'<div class="card" style="padding:8px 12px;">{cex_rows()}</div>'
        '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout("Crypto Holdings", body)


def render_liability(data: dict) -> str:
    def acct_card(a):
        plans_html = ""
        for p in a["plans"]:
            plans_html += (
                f'<div class="card-row" style="padding-left:12px;">'
                f'<span class="name muted" style="font-size:11px;">{p["plan_code"][:40]}</span>'
                f'<span class="sub">{p["remaining_months"]}mo · SGD {p["monthly"]:.2f}/mo</span></div>'
            )
        return (
            '<div class="card">'
            f'<div class="card-row"><span class="name"><b>{a["short_name"]}</b><div class="sub">'
            f'{("Credit limit $" + f"{a['credit_limit']:,.0f}") if a.get("credit_limit") else ""}'
            f'{(" · billing " + str(a["billing_day"])) if a.get("billing_day") else ""}</div></span>'
            f'<span class="amt"><b class="neg">SGD {a["outstanding"]:,.2f}</b>'
            f'<div class="sub">SGD {a["monthly_total"]:,.2f}/mo</div></span></div>'
            f'{plans_html}'
            '</div>'
        )

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        f'<h1>{data["title"]}</h1>'
        f'<div class="big neg">SGD {data["total_outstanding"]:,.2f}</div>'
        f'<div class="subtotal">Monthly obligation: SGD {data["total_monthly"]:,.2f}</div>'
        f'<div class="section-label">{len(data["accounts"])} accounts</div>'
        + "".join(acct_card(a) for a in data["accounts"])
        + '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout(data["title"], body)


def render_funds(data: dict) -> str:
    funds_html = ""
    for f in data["funds"]:
        stale_badge = ' <span style="color:#ffcc00;font-size:10px;">STALE</span>' if f["stale"] else ""
        rows = ""
        for h in f["holdings"]:
            rows += (
                f'<div class="card-row" style="padding-left:12px;font-size:11px;">'
                f'<span class="name muted">{h["policy"]}</span>'
                f'<span class="amt muted">{h["units"]} u · SGD {h["value_sgd"]:,.2f}</span></div>'
            )
        funds_html += (
            '<div class="card">'
            f'<div class="card-row"><span class="name"><b>{f["name"]}</b>'
            f'<div class="sub">{f["currency"]} {f["nav"]:.4f} · {f["nav_date"]} (age {f["age_days"]}d){stale_badge}</div>'
            f'</span><span class="amt"><b>SGD {f["total_sgd"]:,.2f}</b>'
            f'<div class="sub">{f["total_units"]} units</div></span></div>'
            f'{rows}</div>'
        )
    policy_html = "".join(
        f'<div class="card-row"><span class="name">{p["name"]}</span><span class="amt"><b>SGD {p["sgd"]:,.2f}</b></span></div>'
        for p in data["policies"]
    )
    stale_warn = ""
    if data["stale_count"] > 0:
        stale_warn = f'<div style="background:rgba(255,204,0,0.10);border:1px solid #ffcc00;border-radius:8px;padding:10px;color:#ffcc00;font-size:12px;margin-bottom:14px;">{data["stale_count"]} fund NAV(s) over 30 days old — refresh from policy statement when convenient.</div>'
    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>Fund Universe</h1>'
        f'<div class="big">SGD {data["total_sgd"]:,.2f}</div>'
        f'<div class="subtotal">{data["fund_count"]} unique funds across {len(data["policies"])} policies · FX@{data["fx_usd_to_sgd"]}</div>'
        + stale_warn
        + '<div class="section-label">By policy</div>'
        f'<div class="card">{policy_html}</div>'
        + f'<div class="section-label">All funds ({data["fund_count"]})</div>'
        + funds_html
        + '<footer>By Azfar · Powered by Claude · Edit finance/funds.yaml for unit holdings</footer>'
    )
    return _layout("Fund Universe", body)


def render_recurring(data: dict) -> str:
    bucket_html = ""
    for b in data["buckets"]:
        rows_html = ""
        for r in b["items"]:
            day_str = f' · day {r["day"]}' if r.get("day") else ""
            rows_html += (
                f'<div class="card-row"><span class="name">{r["name"]}'
                f'<div class="sub">{r["category"]}{day_str}</div></span>'
                f'<span class="amt"><b>SGD {r["amount"]:.2f}</b>/mo</span></div>'
            )
        bucket_html += (
            f'<div class="section-label">{b["label"]} — SGD {b["subtotal"]:,.2f}/mo · {len(b["items"])} items</div>'
            f'<div class="card">{rows_html}</div>'
        )

    income_rows = "".join(
        f'<div class="card-row"><span class="name">{i["name"]}<div class="sub">day {i.get("day", "?")}</div></span>'
        f'<span class="amt pos"><b>+SGD {i["amount"]:.2f}</b>/mo</span></div>'
        for i in data["income"]
    )

    net_cls = "pos" if data["net_monthly"] >= 0 else "neg"

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>Monthly Recurring</h1>'
        f'<div class="big">SGD {data["grand_total"]:,.2f}<span class="subtotal" style="font-size:12px;">/mo outflow</span></div>'
        f'<div class="subtotal">Income SGD {data["income_total"]:,.2f}/mo · '
        f'<span class="{net_cls}">Net SGD {data["net_monthly"]:+,.2f}/mo</span></div>'

        + '<div class="section-label">Income</div>'
        + f'<div class="card">{income_rows or "<p class=muted>None</p>"}</div>'
        + bucket_html
        + '<p class="muted" style="text-align:center;font-size:11px;margin-top:18px;">'
        'ILP premiums are transfers to asset accounts (Tokio Marine ILP, Singlife Savvy Invest) — cash leaves POSB but builds investments; not true expenses.'
        '</p>'
        + '<footer>By Azfar · Powered by Claude · Edit finance/recurring.yaml or use Cash Forecast → Add Recurring</footer>'
    )
    return _layout("Monthly Recurring", body)
