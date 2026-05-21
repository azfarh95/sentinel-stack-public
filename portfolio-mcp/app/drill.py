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


# V2.22 cleanup: legacy Firefly helpers removed. drill.py reads only from
# canonical SoT (Gate 5 / credit_facilities / balance_sheet) — see
# build_liability_drill, build_bank_drill, etc. inv29 enforces this.


# ── Builders ─────────────────────────────────────────────────────────────────

async def build_bank_drill(days: int = 60) -> dict:
    """Bank drill — reads from build_balance_sheet() so totals always match the
    home glance. Transactions come from GL via category_drill."""
    from . import balance_sheet as bs
    from . import category_drill as cd
    from sqlalchemy import text
    from . import database as db

    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()

    bs_data = await bs.build_balance_sheet()
    fx = float(bs_data.get("usd_to_sgd", 1.27))

    def _find(nodes, target):
        for n in nodes:
            if n.get("id") == target: return n
            r = _find(n.get("children", []), target)
            if r: return r
        return None

    cash_node = _find(bs_data["assets"]["current"]["nodes"], "cash_and_bank") or {"children": [], "sgd": 0}
    total_sgd = float(cash_node.get("sgd", 0) or 0)

    # Collect CoA codes per child so we can pull tx + last-statement info
    breakdown: list[dict] = []
    bank_coa_codes: list[str] = []
    for child in cash_node.get("children", []):
        codes = []
        for it in (child.get("items") or []):
            # universal items carry the parent CoA via the config; we read
            # config codes from the parent node items' raw_balance? No — we
            # need the codes from config not items. Pull them from the config.
            pass
        breakdown.append({
            "name": child.get("label", child.get("id", "?")),
            "sgd": round(float(child.get("sgd", 0) or 0), 2),
            "statement_sgd": None,
            "variance_sgd": None,
            "statement_at": None,
        })

    # Pull bank-account transactions from GL
    cfg = yaml.safe_load(open("/finance/balance_sheet_config.yaml"))
    cash_cfg = None
    for n in cfg.get("assets", {}).get("current", []):
        if n.get("id") == "cash_and_bank":
            cash_cfg = n; break
    if cash_cfg:
        for child in cash_cfg.get("children", []):
            for c in child.get("gl_account_codes", []):
                bank_coa_codes.append(c)

    all_txs = []
    if bank_coa_codes:
        s = db.SessionLocal()
        try:
            placeholders = ",".join([f":c{i}" for i in range(len(bank_coa_codes))])
            params = {f"c{i}": c for i, c in enumerate(bank_coa_codes)}
            params["start"] = start
            params["end"] = end
            rows = s.execute(text(f"""
              SELECT j.journal_date, j.id, j.narration, j.journal_type, j.source_doc,
                     gl.debit_sgd, gl.credit_sgd, coa.account_code, coa.account_name
              FROM general_ledger gl
              JOIN journals j ON j.id = gl.journal_id
              JOIN chart_of_accounts coa ON coa.id = gl.account_id
              WHERE coa.account_code IN ({placeholders})
                AND j.status = 'posted'
                AND j.journal_date BETWEEN :start AND :end
              ORDER BY j.journal_date DESC, j.id DESC
              LIMIT 500
            """), params).fetchall()
            for r in rows:
                signed = float(r[5] or 0) - float(r[6] or 0)  # +Dr = inflow for asset
                all_txs.append({
                    "date": str(r[0])[:10],
                    "amt_signed": round(signed, 2),
                    "type": r[3] or "",
                    "desc": (r[2] or "")[:60],
                    "src": (r[4] or "")[:25],
                    "dst": "",
                    "cat": r[8] or "—",
                    "account": (r[8] or "")[:15],
                })
        finally:
            s.close()

    return {
        "title": "Bank Balance",
        "period_days": days,
        "start": start, "end": end,
        "current_balance_sgd": round(total_sgd, 2),
        "current_balance_usd": round(total_sgd / fx, 2),
        "breakdown": breakdown,
        "transactions": all_txs[:200],
    }


def _latest_statement_for(account_id: int) -> dict | None:
    """Most recent ImportLog row for the given Firefly account. None if no imports yet."""
    try:
        from . import database as db
        s = db.SessionLocal()
        try:
            row = (s.query(db.ImportLog)
                     .filter(db.ImportLog.account_id == account_id,
                             db.ImportLog.ledger_balance.isnot(None))
                     .order_by(db.ImportLog.started_at.desc())
                     .first())
            if not row:
                return None
            return {
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "ledger_balance": row.ledger_balance,
                "variance": row.variance,
                "firefly_balance": row.firefly_balance,
                "file_name": row.file_name,
            }
        finally:
            s.close()
    except Exception:
        logger.exception("_latest_statement_for failed")
        return None


async def build_crypto_drill() -> dict:
    """Crypto drill — totals MUST match the home glance.
    Home crypto = crypto_wallets + defi + token_holdings + staking_vaults
    from balance_sheet_config.yaml. This builder routes through the same
    balance_sheet.build_balance_sheet() so totals always agree (Task #46).
    """
    from . import balance_sheet as bs
    bs_data = await bs.build_balance_sheet()
    fx = float(bs_data.get("usd_to_sgd", 1.27))

    cur_nodes = bs_data["assets"]["current"]["nodes"]
    nc_nodes = bs_data["assets"]["non_current"]["nodes"]

    def _find(nodes, target):
        for n in nodes:
            if n.get("id") == target:
                return n
            r = _find(n.get("children", []), target)
            if r:
                return r
        return None

    crypto_wallets = _find(cur_nodes, "crypto_wallets") or {"usd": 0, "sgd": 0, "items": []}
    defi = _find(cur_nodes, "defi") or {"usd": 0, "sgd": 0, "items": []}
    tokens = _find(cur_nodes, "token_holdings") or {"usd": 0, "sgd": 0, "items": []}
    staking = _find(nc_nodes, "staking_vaults") or {"usd": 0, "sgd": 0, "items": []}

    total_usd = (crypto_wallets["usd"] + defi["usd"] + tokens["usd"] + staking["usd"])
    total_sgd = (crypto_wallets["sgd"] + defi["sgd"] + tokens["sgd"] + staking["sgd"])

    def _items(node):
        out = []
        for c in (node.get("children") or []):
            for it in (c.get("items") or []):
                out.append({**it, "section": c.get("label", "?")})
            if not c.get("children") and not c.get("items"):
                if c.get("usd", 0) or c.get("sgd", 0):
                    out.append({"label": c.get("label", "?"),
                                "usd": c.get("usd", 0), "sgd": c.get("sgd", 0),
                                "section": c.get("label", "?")})
        for it in (node.get("items") or []):
            out.append({**it, "section": node.get("label", "?")})
        return out

    # CEX accounts (= crypto_wallets node children: Coinbase, Crypto.com)
    cex_items = _items(crypto_wallets)
    cex = [{"name": x.get("label", "?"),
            "usd": round(x.get("usd", 0), 2), "sgd": round(x.get("sgd", 0), 2)}
           for x in cex_items]

    # Liquid on-chain (token_holdings node)
    liquid = [{"symbol": x.get("label", "?").split(" (")[0],
               "chain": (x.get("label", "?").split("(")[-1].rstrip(")")
                          if "(" in x.get("label", "") else "?"),
               "usd": round(x.get("usd", 0), 2), "sgd": round(x.get("sgd", 0), 2)}
              for x in _items(tokens)]
    liquid.sort(key=lambda x: -x["usd"])

    # Manual / staking / LP (defi + staking_vaults nodes)
    manual = []
    for x in _items(defi) + _items(staking):
        manual.append({
            "label": x.get("label", "?"),
            "protocol": x.get("section") or "—",
            "chain": x.get("chain") or "?",
            "usd": round(x.get("usd", 0), 2),
            "sgd": round(x.get("sgd", 0), 2),
        })
    manual.sort(key=lambda x: -x["usd"])

    return {
        "title": "Crypto Holdings",
        "fx": fx,
        "totals": {
            "usd": round(total_usd, 2),
            "sgd": round(total_sgd, 2),
        },
        "section_totals": {
            "cex_sgd": round(crypto_wallets["sgd"], 2),
            "liquid_sgd": round(tokens["sgd"], 2),
            "defi_sgd": round(defi["sgd"], 2),
            "staking_sgd": round(staking["sgd"], 2),
        },
        "liquid_positions": liquid,
        "manual_positions": manual,
        "cex_accounts": cex,
    }


async def build_liability_drill(only_type: str | None = None) -> dict:
    """Group by registry account. only_type: 'credit_card' for CC drill, else loans (everything else).

    Audit-V2 fix: current outstanding now reads from `credit_facilities`
    table (the canonical SoT, same path as /facilities). Previously read
    from Firefly III which is decoupled — left a $482 discrepancy on UOB
    CashPlus between /loans and /facilities pages.
    """
    reg = yaml.safe_load(open(LIAB_PATH))

    # Build {facility_id → current_outstanding} map from credit_facilities once.
    from . import database as _db
    from sqlalchemy import select as _select
    fac_outstanding: dict[str, float] = {}
    fac_available: dict[str, float | None] = {}
    s = _db.SessionLocal()
    try:
        for f in s.execute(_select(_db.CreditFacility)).scalars().all():
            fac_outstanding[f.id] = float(f.current_outstanding or 0)
            fac_available[f.id] = (
                float(f.available_balance) if f.available_balance is not None else None
            )
    finally:
        s.close()

    rows = []
    for acct in reg["accounts"]:
        atype = acct.get("type", "")
        if only_type == "credit_card" and atype != "credit_card": continue
        if only_type == "loans" and atype == "credit_card": continue

        # Single source of truth: credit_facilities table.
        # Fall back to registry YAML only if the facility isn't in the DB yet.
        fac_id = acct.get("id")
        if fac_id in fac_outstanding:
            current = fac_outstanding[fac_id]
        else:
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


async def build_ilp_drill() -> dict:
    """ILP detail page.

    For each policy (Tokio Marine ILP, Singlife Savvy Invest):
      * Firefly cash-value balance (per balance_sheet_config.yaml ILP node)
      * Per-fund unit holdings from funds.yaml with current NAV
      * Monthly premium (recurring.yaml)
    Then a reconciliation between Firefly balance and computed fund value.
    """
    cfg = yaml.safe_load(open("/finance/balance_sheet_config.yaml"))
    funds_data = yaml.safe_load(open("/finance/funds.yaml"))
    recurring = yaml.safe_load(open("/finance/recurring.yaml"))
    fx = float(cfg.get("usd_to_sgd", 1.27))

    # Locate ILP node and its children
    ilp_node = None
    for n in cfg.get("assets", {}).get("non_current", []):
        if n.get("id") == "ilp":
            ilp_node = n
            break
    if not ilp_node:
        return {"policies": [], "grand_total_sgd": 0.0, "fx_usd_to_sgd": fx,
                "error": "ILP node not found in balance_sheet_config.yaml"}

    # Pull per-policy balances from GL via _gl_balances (using gl_account_codes
    # in the config). Replaces Firefly account fetch.
    from . import balance_sheet as bs
    policy_balances: dict[str, float] = {}
    for child in ilp_node.get("children", []):
        codes = child.get("gl_account_codes", [])
        bal = 0.0
        for _code, _name, b in bs._gl_balances(codes):
            bal += float(b or 0)
        policy_balances[child["label"]] = bal

    # Premium map by policy name (from recurring.yaml)
    premium_map = {}
    for e in recurring.get("expense", []):
        cat = (e.get("category") or "").lower()
        if "ilp" in cat:
            premium_map[e["name"]] = float(e["amount"])

    # For each child policy, list funds that hold for it
    policies = []
    for child in ilp_node.get("children", []):
        label = child["label"]
        firefly_sgd = policy_balances.get(label, 0.0)  # variable name kept; now GL-sourced
        funds_in_policy = []
        computed_sgd = 0.0
        for f in funds_data.get("funds", []):
            nav = float(f.get("last_nav") or 0)
            ccy = f.get("currency", "SGD")
            for h in f.get("holdings", []):
                if h.get("policy", "").lower() == label.lower():
                    units = float(h["units"])
                    ccy_value = units * nav
                    sgd_value = ccy_value * (fx if ccy == "USD" else 1.0)
                    computed_sgd += sgd_value
                    funds_in_policy.append({
                        "name": f["name"],
                        "currency": ccy,
                        "nav": nav,
                        "nav_date": f.get("last_nav_date", ""),
                        "units": round(units, 5),
                        "value_ccy": round(ccy_value, 2),
                        "value_sgd": round(sgd_value, 2),
                    })
        funds_in_policy.sort(key=lambda x: -x["value_sgd"])

        # Best-match for premium: substring match
        premium = 0.0
        for prem_name, amt in premium_map.items():
            if any(tok in prem_name.lower() for tok in label.lower().split()[:2]):
                premium = amt
                break

        diff_sgd = computed_sgd - firefly_sgd
        diff_pct = (diff_sgd / firefly_sgd * 100) if firefly_sgd else None
        policies.append({
            "id": child["id"],
            "label": label,
            "firefly_sgd": round(firefly_sgd, 2),
            "computed_sgd": round(computed_sgd, 2),
            "diff_sgd": round(diff_sgd, 2),
            "diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
            "premium_monthly_sgd": premium,
            "fund_count": len(funds_in_policy),
            "funds": funds_in_policy,
        })

    grand_firefly = sum(p["firefly_sgd"] for p in policies)
    grand_computed = sum(p["computed_sgd"] for p in policies)
    grand_premium = sum(p["premium_monthly_sgd"] for p in policies)
    return {
        "title": "ILP Investments",
        "policies": policies,
        "grand_firefly_sgd": round(grand_firefly, 2),
        "grand_computed_sgd": round(grand_computed, 2),
        "grand_premium_monthly_sgd": round(grand_premium, 2),
        "fx_usd_to_sgd": fx,
    }


async def build_cpf_drill() -> dict:
    """CPF detail page.

    Per-account balances (OA / SA / MA / IS) from the GL via the same
    `_gl_balances` helper the home glance uses, plus IS fund breakdown
    if any holdings exist under policy 'CPF-IS' in funds.yaml.
    """
    cfg = yaml.safe_load(open("/finance/balance_sheet_config.yaml"))
    funds_data = yaml.safe_load(open("/finance/funds.yaml"))
    fx = float(cfg.get("usd_to_sgd", 1.27))

    cpf_node = None
    for n in cfg.get("assets", {}).get("non_current", []):
        if n.get("id") == "cpf":
            cpf_node = n
            break
    if not cpf_node:
        return {"accounts": [], "grand_total_sgd": 0.0,
                "error": "CPF node not found in balance_sheet_config.yaml"}

    accounts = []
    is_account = None
    for child in cpf_node.get("children", []):
        bal_sgd = 0.0
        codes = child.get("gl_account_codes", [])
        if codes:
            for _code, _name, bal in bs._gl_balances(codes):
                bal_sgd += float(bal or 0)
        entry = {
            "id": child["id"],
            "label": child["label"],
            "sgd": round(bal_sgd, 2),
            "gl_account_codes": codes,
        }
        accounts.append(entry)
        if "is" in child["id"]:  # cpf_is
            is_account = entry

    grand = sum(a["sgd"] for a in accounts)
    for a in accounts:
        a["pct"] = round((a["sgd"] / grand * 100) if grand else 0.0, 1)

    # CPF IS fund breakdown (if any funds have a "CPF-IS" or similar policy)
    is_funds: list = []
    is_computed_sgd = 0.0
    if is_account:
        for f in funds_data.get("funds", []):
            nav = float(f.get("last_nav") or 0)
            ccy = f.get("currency", "SGD")
            for h in f.get("holdings", []):
                pname = h.get("policy", "")
                if "cpf" in pname.lower() and ("is" in pname.lower() or "invest" in pname.lower()):
                    units = float(h["units"])
                    ccy_value = units * nav
                    sgd_value = ccy_value * (fx if ccy == "USD" else 1.0)
                    is_computed_sgd += sgd_value
                    is_funds.append({
                        "name": f["name"],
                        "currency": ccy,
                        "nav": nav,
                        "nav_date": f.get("last_nav_date", ""),
                        "units": round(units, 5),
                        "value_ccy": round(ccy_value, 2),
                        "value_sgd": round(sgd_value, 2),
                    })
        is_funds.sort(key=lambda x: -x["value_sgd"])

    return {
        "title": "CPF (incl. IS)",
        "accounts": accounts,
        "grand_total_sgd": round(grand, 2),
        "is_account": is_account,
        "is_funds": is_funds,
        "is_computed_sgd": round(is_computed_sgd, 2),
        "fx_usd_to_sgd": fx,
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
        f'<link rel="stylesheet" href="/static/privacy.css">'
        f'<style>{_CSS}</style>'
        f'<script src="/static/privacy.js" defer></script>'
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

    # Per-account row with optional statement-balance variance indicator
    def _acct_row(b: dict) -> str:
        live_html = f'<span class="amt">SGD {b["sgd"]:,.2f}</span>'
        stmt = b.get("statement_sgd")
        var = b.get("variance_sgd")
        if stmt is None or var is None:
            return (f'<div class="card-row"><span class="name">{b["name"]}</span>'
                    f'{live_html}</div>')
        # Variance pill
        if abs(var) < 0.50:
            cls, label = "pos", "matches statement"
        elif abs(var) < 5.00:
            cls, label = "muted", f"{var:+.2f} drift"
        else:
            cls, label = "neg", f"{var:+.2f} drift"
        when = (b.get("statement_at") or "")[:10]
        return (
            f'<div class="card-row"><span class="name">{b["name"]}'
            f'<div class="muted" style="font-size:10px;">'
            f'Statement SGD {stmt:,.2f} as at {when} · '
            f'<span class="{cls}">{label}</span></div></span>'
            f'{live_html}</div>'
        )

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>Bank Balance</h1>'
        f'<div class="big">SGD {data["current_balance_sgd"]:,.2f}</div>'
        '<div class="card">'
        + "".join(_acct_row(b) for b in data["breakdown"])
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


def render_ilp(data: dict) -> str:
    if data.get("error"):
        body = (f'<a class="back" href="/">&larr; Home</a><h1>ILP Investments</h1>'
                f'<p class="muted">{data["error"]}</p>')
        return _layout("ILP Investments", body)

    fx = data["fx_usd_to_sgd"]
    policy_sections = ""
    for p in data["policies"]:
        funds_html = ""
        if p["funds"]:
            funds_html = (
                '<table><thead><tr><th>Fund</th>'
                '<th class="amt">Units</th><th class="amt">NAV</th>'
                '<th class="amt">SGD</th></tr></thead><tbody>'
            )
            for f in p["funds"]:
                ccy = f["currency"]
                nav_display = f'{ccy} {f["nav"]:.4f}'
                funds_html += (
                    f'<tr><td>{f["name"]}'
                    f'<div class="muted" style="font-size:10px;">NAV as at {f["nav_date"]}</div></td>'
                    f'<td class="amt">{f["units"]:,.4f}</td>'
                    f'<td class="amt muted">{nav_display}</td>'
                    f'<td class="amt"><b>${f["value_sgd"]:,.2f}</b></td></tr>'
                )
            funds_html += '</tbody></table>'
        else:
            funds_html = '<p class="muted" style="padding:8px;font-size:12px;">No fund holdings in funds.yaml for this policy</p>'

        diff_html = ""
        if p["diff_pct"] is not None:
            cls = "muted"
            if abs(p["diff_pct"]) > 3.0:
                cls = "neg" if p["diff_sgd"] < 0 else "pos"
            diff_html = (
                f'<div class="card-row"><span class="name muted">Variance (computed − Firefly)</span>'
                f'<span class="amt {cls}">{p["diff_sgd"]:+,.2f} ({p["diff_pct"]:+.2f}%)</span></div>'
            )

        prem = p["premium_monthly_sgd"]
        prem_html = (
            f'<div class="card-row"><span class="name muted">Monthly premium</span>'
            f'<span class="amt">SGD {prem:,.2f}</span></div>'
        ) if prem else ""

        policy_sections += (
            f'<details class="card collapse-card">'
            '<summary>'
            f'<span class="name"><b>{p["label"]}</b>'
            f'<div class="sub">{p["fund_count"]} funds · '
            f'<span class="amt">SGD {prem:,.2f}</span>/mo premium</div></span>'
            f'<span class="amt"><b>SGD {p["firefly_sgd"]:,.2f}</b></span>'
            '</summary>'
            f'<div class="card-row"><span class="name muted">Computed (units × NAV)</span>'
            f'<span class="amt">SGD {p["computed_sgd"]:,.2f}</span></div>'
            + diff_html
            + f'<div class="card-row"><span class="name muted">Monthly premium</span>'
            f'<span class="amt">SGD {prem:,.2f}</span></div>'
            + f'<div style="padding:8px 12px;">{funds_html}</div>'
            '</details>'
        )

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>ILP Investments</h1>'
        f'<div class="big">SGD {data["grand_firefly_sgd"]:,.2f}</div>'
        f'<div class="subtotal">Premiums SGD {data["grand_premium_monthly_sgd"]:,.2f}/mo · FX@{fx}</div>'
        + policy_sections +
        '<footer>By Azfar · Powered by Claude · Edit finance/funds.yaml + balance_sheet_config.yaml</footer>'
    )
    return _layout("ILP Investments", body)


def render_cpf(data: dict) -> str:
    if data.get("error"):
        body = (f'<a class="back" href="/">&larr; Home</a><h1>CPF</h1>'
                f'<p class="muted">{data["error"]}</p>')
        return _layout("CPF", body)

    acct_rows = ""
    for a in data["accounts"]:
        acct_rows += (
            f'<div class="card-row"><span class="name">{a["label"]}'
            f'<div class="muted" style="font-size:10px;"><span class="amt">{a["pct"]}</span>% of total</div></span>'
            f'<span class="amt"><b>SGD {a["sgd"]:,.2f}</b></span></div>'
        )

    is_section = ""
    if data["is_account"]:
        is_acct = data["is_account"]
        funds = data["is_funds"]
        computed = data["is_computed_sgd"]
        diff = computed - is_acct["sgd"]
        diff_pct = (diff / is_acct["sgd"] * 100) if is_acct["sgd"] else 0.0
        if funds:
            funds_html = '<table><thead><tr><th>Fund</th><th class="amt">Units</th><th class="amt">NAV</th><th class="amt">SGD</th></tr></thead><tbody>'
            for f in funds:
                funds_html += (
                    f'<tr><td>{f["name"]}<div class="muted" style="font-size:10px;">NAV {f["nav_date"]}</div></td>'
                    f'<td class="amt">{f["units"]:,.4f}</td>'
                    f'<td class="amt muted">{f["currency"]} {f["nav"]:.4f}</td>'
                    f'<td class="amt"><b>${f["value_sgd"]:,.2f}</b></td></tr>'
                )
            funds_html += '</tbody></table>'
        else:
            funds_html = '<p class="muted" style="padding:8px;font-size:12px;">No CPF-IS fund holdings in funds.yaml yet</p>'

        diff_cls = "muted"
        if abs(diff_pct) > 3.0:
            diff_cls = "neg" if diff < 0 else "pos"
        is_section = (
            '<div class="section-label">CPF Investment Scheme holdings</div>'
            '<div class="card">'
            f'<div class="card-row"><span class="name">GL CPF IS balance</span>'
            f'<span class="amt"><b>SGD {is_acct["sgd"]:,.2f}</b></span></div>'
            f'<div class="card-row"><span class="name muted">Computed from units × NAV</span>'
            f'<span class="amt">SGD {computed:,.2f}</span></div>'
            f'<div class="card-row"><span class="name muted">Variance</span>'
            f'<span class="amt {diff_cls}">{diff:+,.2f} ({diff_pct:+.2f}%)</span></div>'
            '</div>'
            f'<div class="card" style="padding:8px 12px;">{funds_html}</div>'
        )

    body = (
        '<a class="back" href="/">&larr; Home</a>'
        '<h1>CPF (incl. IS)</h1>'
        f'<div class="big">SGD {data["grand_total_sgd"]:,.2f}</div>'
        f'<div class="subtotal">Across {len(data["accounts"])} accounts</div>'
        '<div class="section-label">Per-account breakdown</div>'
        '<div class="card">'
        + acct_rows +
        '</div>'
        + is_section +
        '<footer>By Azfar · Powered by Claude · Edit finance/balance_sheet_config.yaml for account mapping</footer>'
    )
    return _layout("CPF", body)


def render_liability(data: dict) -> str:
    def acct_card(a):
        plans_html = ""
        for p in a["plans"]:
            plans_html += (
                f'<div class="card-row" style="padding-left:12px;">'
                f'<span class="name muted" style="font-size:11px;">{p["plan_code"][:40]}</span>'
                f'<span class="amt" style="font-size:11px;color:var(--muted);">'
                f'{p["remaining_months"]}mo · SGD {p["monthly"]:.2f}/mo</span></div>'
            )
        meta_bits = []
        if a.get("credit_limit"):
            meta_bits.append(f'Credit limit $<span class="amt">{a["credit_limit"]:,.0f}</span>')
        if a.get("billing_day"):
            meta_bits.append(f'billing <span class="amt">{a["billing_day"]}</span>')
        meta_html = (' · '.join(meta_bits))
        return (
            '<details class="card collapse-card">'
            '<summary>'
            f'<span class="name"><b>{a["short_name"]}</b>'
            f'<div class="sub">{meta_html}</div></span>'
            f'<span class="amt"><b class="neg">SGD {a["outstanding"]:,.2f}</b>'
            f'<div class="amt" style="font-size:11px;color:var(--muted);">SGD {a["monthly_total"]:,.2f}/mo</div></span>'
            '</summary>'
            f'{plans_html}'
            '</details>'
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
            '<details class="card collapse-card">'
            '<summary>'
            f'<span class="name"><b>{f["name"]}</b>'
            f'<div class="sub">{f["currency"]} <span class="amt">{f["nav"]:.4f}</span> · {f["nav_date"]} (age {f["age_days"]}d){stale_badge}</div>'
            f'</span><span class="amt"><b>SGD {f["total_sgd"]:,.2f}</b>'
            f'<div class="amt" style="font-size:11px;color:var(--muted);">{f["total_units"]} units</div></span>'
            '</summary>'
            f'{rows}</details>'
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
