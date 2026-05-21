"""Sentinel Finance home dashboard + config sub-pages.

Layout (per user's 2026-05-12 sketch):
  Header   — "Sentinel Finance" + privacy toggle
  Glance   — 4 customisable summary cards (Bank, Crypto, Loans, CC)
  Tiles    — 4 navigation tiles (Balance Sheet, Income Statement, Budget, Cash Forecast)
  Config   — gear-icon link
  Footer   — "By Azfar · Powered by Claude"

Coming-soon tiles: Income Statement, Budget, Cash Forecast.
Wired tile: Balance Sheet.
"""
import os
import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import balance_sheet as bs
from . import connectors as conn_mod
from . import fx as fx_mod

LIABILITIES_PATH = "/finance/liabilities-registry.yaml"


async def build_home_summary() -> dict:
    """Compute the glance-card numbers + net worth from build_balance_sheet."""
    data = await bs.build_balance_sheet()
    fx = float(data.get("usd_to_sgd", 1.27))

    def node_by_id(nodes, target):
        for n in nodes:
            if n.get("id") == target:
                return n
            r = node_by_id(n.get("children", []), target)
            if r:
                return r
        return None

    cur_nodes = data["assets"]["current"]["nodes"]
    nc_nodes = data["assets"]["non_current"]["nodes"]

    bank = node_by_id(cur_nodes, "cash_and_bank") or {"usd": 0, "sgd": 0}
    crypto_wallets = node_by_id(cur_nodes, "crypto_wallets") or {"usd": 0, "sgd": 0}
    defi = node_by_id(cur_nodes, "defi") or {"usd": 0, "sgd": 0}
    tokens = node_by_id(cur_nodes, "token_holdings") or {"usd": 0, "sgd": 0}
    staking = node_by_id(nc_nodes, "staking_vaults") or {"usd": 0, "sgd": 0}

    crypto_usd = crypto_wallets["usd"] + defi["usd"] + tokens["usd"] + staking["usd"]
    crypto_sgd = crypto_wallets["sgd"] + defi["sgd"] + tokens["sgd"] + staking["sgd"]

    # ILP investments (Tokio Marine + Singlife Savvy)
    ilp_node = node_by_id(nc_nodes, "ilp") or {"usd": 0, "sgd": 0}
    # CPF totals (OA + SA + MA + IS combined)
    cpf_node = node_by_id(nc_nodes, "cpf") or {"usd": 0, "sgd": 0}

    # Classify liabilities by registry type — use LIVE Firefly balances (registry's
    # current_outstanding is the snapshot, doesn't reflect ongoing payments)
    try:
        reg = yaml.safe_load(open(LIABILITIES_PATH))
    except FileNotFoundError:
        reg = {"accounts": []}
    # Map firefly_acct_id → current balance from balance_sheet API
    import httpx
    ff_balances = {}
    pat = os.environ.get("FIREFLY_PAT", "")
    if pat:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{bs.FIREFLY_URL}/api/v1/accounts?type=liabilities&limit=200",
                                headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"})
                for a in r.json().get("data", []):
                    ff_balances[int(a["id"])] = abs(float(a["attributes"]["current_balance"]))
        except Exception:
            pass
    cc_sgd = 0.0
    loan_sgd = 0.0
    for acct in reg.get("accounts", []):
        live = ff_balances.get(int(acct.get("firefly_acct_id", 0)),
                               float(acct.get("current_outstanding", 0)))
        if acct.get("type") == "credit_card":
            cc_sgd += live
        else:
            loan_sgd += live
    cc_usd = cc_sgd / fx
    loan_usd = loan_sgd / fx

    # Monthly recurring — single source of truth is finance/recurring.yaml
    # (covers insurance + debt service + ILP premiums; auto-grown via /cash_forecast)
    recurring_sgd = 0.0
    try:
        sched = yaml.safe_load(open("/finance/recurring.yaml"))
        for e in (sched.get("expense") or []):
            if e.get("enabled", True):
                recurring_sgd += float(e.get("amount", 0))
    except Exception:
        pass

    return {
        "generated_at_utc": data["generated_at_utc"],
        "fx": fx,
        "bank":   {"usd": round(bank["usd"], 2),   "sgd": round(bank["sgd"], 2)},
        "crypto": {"usd": round(crypto_usd, 2),   "sgd": round(crypto_sgd, 2)},
        "ilp":    {"usd": round(ilp_node["usd"], 2), "sgd": round(ilp_node["sgd"], 2)},
        "cpf":    {"usd": round(cpf_node["usd"], 2), "sgd": round(cpf_node["sgd"], 2)},
        "loans":  {"usd": round(loan_usd, 2),     "sgd": round(loan_sgd, 2)},
        "cc":     {"usd": round(cc_usd, 2),       "sgd": round(cc_sgd, 2)},
        "recurring": {"usd": round(recurring_sgd / fx, 2), "sgd": round(recurring_sgd, 2)},
        "net_worth": {"usd": data["net_worth_usd"], "sgd": data["net_worth_sgd"]},
    }


# ── HTML ─────────────────────────────────────────────────────────────────────

_CSS = """
:root { --bg:#1c1c1e; --fg:#f0f0f0; --muted:#8e8e93; --accent:#4cd964; --sep:rgba(255,255,255,0.10); --pos:#4cd964; --neg:#ff3b30; --card:#2c2c2e; }
* { box-sizing: border-box; }
body { margin:0; padding:18px 14px 60px; background:var(--bg); color:var(--fg);
  font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 560px; margin-left: auto; margin-right: auto; }
header { display:flex; justify-content:space-between; align-items:center; padding:6px 2px 18px; }
h1 { margin:0; font-size:18px; font-weight:700; }
.privacy-btn { background:transparent; border:1px solid var(--sep); color:var(--fg);
  width:38px; height:38px; border-radius:50%; font-size:16px; cursor:pointer; }
.privacy-btn:hover { background:rgba(255,255,255,0.05); }
body.private .amt { filter: blur(8px); transition: filter 0.25s; }
body:not(.private) .amt { filter: none; transition: filter 0.25s; }
.section-label { font-size:11px; text-transform:uppercase; letter-spacing:0.7px;
  color:var(--muted); margin: 14px 4px 8px; font-weight:600; }
.glance { background:var(--card); border-radius:14px; padding:16px;
  border:1px solid var(--sep); }
.glance-title { font-size:12px; color:var(--muted); margin: 0 0 12px; }
.glance-grid { display:grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
.glance-cell { display:flex; flex-direction:column; color: var(--fg); text-decoration: none;
  padding: 4px 6px; margin: -4px -6px; border-radius: 6px;
  transition: background 0.15s; }
a.glance-cell:hover, a.glance-cell:active { background: rgba(255,255,255,0.05); }
.glance-cell .k { font-size:11px; color:var(--muted); margin-bottom:2px; }
.glance-cell .amt { font-size:15px; font-weight:600; font-variant-numeric: tabular-nums; }
.glance-cell .usd { font-size:10px; color:var(--muted); margin-top:1px; font-variant-numeric: tabular-nums; }
.glance-cell.networth .amt { color: var(--pos); font-size: 18px; }
.tiles { display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-top:6px; }
.tile { display:block; background:var(--card); border:1px solid var(--sep); border-radius:12px;
  padding:14px; text-decoration:none; color:var(--fg); transition: background 0.15s, transform 0.15s; }
.tile:hover { background:rgba(255,255,255,0.04); }
.tile:active { transform: scale(0.97); }
.tile .ico { font-size: 22px; opacity: 0.9; margin-bottom: 6px; }
.tile .name { font-size:13px; font-weight:600; }
.tile .sub { font-size:10px; color:var(--muted); margin-top:2px; }
.tile.coming-soon .sub { color:#ffcc00; opacity: 0.7; }
.tile.full-width { grid-column: span 2; }
.config-row { display:flex; align-items:center; gap:8px; padding: 12px 14px;
  background:var(--card); border:1px solid var(--sep); border-radius:10px; margin-top:6px;
  text-decoration:none; color: var(--fg); }
.config-row .name { flex:1; font-size:13px; }
.config-row .sub { font-size:10px; color:var(--muted); }
.config-row .ico { font-size:16px; opacity:0.8; }
footer { color:var(--muted); font-size:10px; text-align:center; margin-top:24px; }
.meta { color:var(--muted); font-size: 10px; margin-bottom: 14px; }
.back { display:inline-block; color:var(--accent); font-size:13px; text-decoration:none; margin-bottom:8px; }
.todo-banner { background: rgba(255,204,0,0.08); border:1px dashed rgba(255,204,0,0.3); border-radius:8px;
  padding:14px; color:#ffcc00; font-size:12px; text-align:center; margin-top:18px; }
.todo-banner b { color: #fff; }
"""


def _layout(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f'<title>{title} — Sentinel Finance</title>'
        f'<link rel="manifest" href="/manifest.webmanifest">'
        f'<meta name="theme-color" content="#1c1c1e">'
        f'<link rel="apple-touch-icon" href="/static/icon-192.png">'
        f'<link rel="icon" href="/static/icon-192.png">'
        f'<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        f'<style>{_CSS}</style>'
        f'<script>'
        f'document.addEventListener("DOMContentLoaded",function(){{'
        f'if(localStorage.getItem("sentinel-private")==="1")document.body.classList.add("private");'
        f'try{{Telegram.WebApp.ready();Telegram.WebApp.expand();}}catch(e){{}}'
        f'}});'
        f'function togglePrivacy(){{document.body.classList.toggle("private");'
        f'localStorage.setItem("sentinel-private",document.body.classList.contains("private")?"1":"0");}}'
        f'</script>'
        f'<script>if("serviceWorker" in navigator){{window.addEventListener("load",()=>navigator.serviceWorker.register("/sw.js"));}}</script>'
        f'</head><body>{body}</body></html>'
    )


def render_home(summary: dict) -> str:
    s = summary
    cell = lambda name, usd, sgd, drill_key=None: (
        f'<a href="/drill/{drill_key}" class="glance-cell"' if drill_key else
        '<div class="glance-cell"'
    ) + (
        '>'
        f'<span class="k">{name}</span>'
        f'<span class="amt">S$ {sgd:,.2f}</span>'
        f'<span class="usd amt">US$ {usd:,.2f}</span>'
        + ('</a>' if drill_key else '</div>')
    )
    glance = (
        '<div class="glance">'
        f'<p class="glance-title">Your portfolio at a glance</p>'
        '<div class="glance-grid">'
        f'{cell("Bank Balance", s["bank"]["usd"], s["bank"]["sgd"], "bank")}'
        f'{cell("Crypto Holdings", s["crypto"]["usd"], s["crypto"]["sgd"], "crypto")}'
        f'{cell("ILP Investments", s["ilp"]["usd"], s["ilp"]["sgd"])}'
        f'{cell("CPF (incl. IS)", s["cpf"]["usd"], s["cpf"]["sgd"])}'
        f'{cell("Total Loans", s["loans"]["usd"], s["loans"]["sgd"], "loans")}'
        f'{cell("Total CC", s["cc"]["usd"], s["cc"]["sgd"], "cc")}'
        '</div>'
        '<div class="glance-grid" style="margin-top:14px;border-top:1px solid var(--sep);padding-top:12px;">'
        f'{cell("Monthly Recurring", s["recurring"]["usd"], s["recurring"]["sgd"], "recurring")}'
        f'<div class="glance-cell networth"><span class="k">Net Worth</span>'
        f'<span class="amt">S$ {s["net_worth"]["sgd"]:,.2f}</span>'
        f'<span class="usd amt">US$ {s["net_worth"]["usd"]:,.2f}</span></div>'
        '</div>'
        '</div>'
    )

    tiles = (
        '<div class="section-label">Reports</div>'
        '<div class="tiles">'
        '  <a class="tile" href="/balance_sheet"><div class="ico">📊</div><div class="name">Balance Sheet</div><div class="sub">Assets, liabilities, net worth</div></a>'
        '  <a class="tile" href="/income_statement"><div class="ico">📈</div><div class="name">Income Statement</div><div class="sub">Income, expenses, net income</div></a>'
        '  <a class="tile coming-soon" href="/coming-soon?p=budget"><div class="ico">🎯</div><div class="name">Budget</div><div class="sub">Coming soon</div></a>'
        '  <a class="tile" href="/cash_forecast"><div class="ico">🌊</div><div class="name">Cash Forecast</div><div class="sub">90-day POSB projection</div></a>'
        '</div>'
    )

    config = (
        '<div class="section-label">Settings</div>'
        '<a class="config-row" href="/config">'
        '<span class="ico">⚙️</span>'
        '<span class="name">Config</span>'
        '<span class="sub">FX rate, integrations, account</span>'
        '</a>'
    )

    body = (
        '<header>'
        '<h1>Sentinel Finance</h1>'
        '<button class="privacy-btn" onclick="togglePrivacy()" title="Hide / show balances">👁</button>'
        '</header>'
        + glance
        + tiles
        + config
        + '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout("Home", body)


def render_config_page(user) -> str:
    name = user.telegram_username or f"id:{user.telegram_user_id}"
    body = (
        '<a class="back" href="/">&larr; Back</a>'
        '<h1>Config</h1>'
        '<p class="meta">Signed in as <b>@' + name + '</b></p>'

        '<div class="section-label">Integrations</div>'
        '<a class="config-row" href="/config/connectors">'
        '<span class="ico">🔌</span>'
        '<span class="name">Connectors</span>'
        '<span class="sub">Live status of Firefly · Wise · Google · Telegram · OneDrive · Moralis</span></a>'
        '<a class="config-row coming-soon" href="/coming-soon?p=gmail">'
        '<span class="ico">📧</span>'
        '<span class="name">Link Gmail</span>'
        '<span class="sub">Pull bank + iFAST transactions</span></a>'
        '<a class="config-row coming-soon" href="/coming-soon?p=drive">'
        '<span class="ico">☁️</span>'
        '<span class="name">Connect to Google Drive</span>'
        '<span class="sub">Upload statements automatically</span></a>'
        '<a class="config-row coming-soon" href="/coming-soon?p=telegram-bot">'
        '<span class="ico">💬</span>'
        '<span class="name">Telegram Bot</span>'
        '<span class="sub">Daily summary + alerts</span></a>'

        '<div class="section-label">Preferences</div>'
        + _fx_config_row() +
        '<a class="config-row coming-soon" href="/coming-soon?p=glance">'
        '<span class="ico">🎛️</span>'
        '<span class="name">Customise Glance Cards</span>'
        '<span class="sub">Pick which 4 numbers show on Home</span></a>'
        '<a class="config-row coming-soon" href="/coming-soon?p=threshold">'
        '<span class="ico">📏</span>'
        '<span class="name">Chain Dust Threshold</span>'
        '<span class="sub">Currently $50 · Balance Sheet → Token Holdings</span></a>'

        '<div class="section-label">Account</div>'
        '<a class="config-row" href="/admin/users">'
        '<span class="ico">👥</span>'
        '<span class="name">Manage Users</span>'
        '<span class="sub">Admin · approve, suspend</span></a>'
        '<a class="config-row" href="/auth/logout">'
        '<span class="ico">🚪</span>'
        '<span class="name">Sign Out</span>'
        '<span class="sub">@' + name + '</span></a>'

        '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout("Config", body)


def _fx_config_row() -> str:
    fx = fx_mod.get_fx()
    return (
        '<a class="config-row" href="/config/fx">'
        '<span class="ico">💱</span>'
        f'<span class="name">FX Rate (USD → SGD): <b>{fx["rate"]:.4f}</b></span>'
        f'<span class="sub">{fx["source"]} · updated {fx["last_updated"]}</span></a>'
    )


def render_fx_page(user, error: str = "", flash: str = "") -> str:
    fx = fx_mod.get_fx()
    flash_html = f'<div style="background:rgba(76,217,100,0.10);border:1px solid var(--accent);border-radius:8px;padding:10px;color:var(--accent);margin:12px 0;font-size:12px;">{flash}</div>' if flash else ""
    error_html = f'<div style="background:rgba(255,59,48,0.10);border:1px solid var(--neg);border-radius:8px;padding:10px;color:var(--neg);margin:12px 0;font-size:12px;">{error}</div>' if error else ""

    options = "".join(
        f'<option value="{s}"{" selected" if s == fx["source"] else ""}>{s}</option>'
        for s in fx_mod.SOURCES
    )
    body = (
        '<a class="back" href="/config">&larr; Back</a>'
        '<h1>FX Rate (USD → SGD)</h1>'
        '<p class="muted">Used by the balance sheet to convert USD totals to SGD. Edit manually or fetch live from a public source.</p>'
        + error_html + flash_html +
        '<div class="config-row" style="display:block;padding:18px;margin-top:14px;">'
        f'<div style="font-size:22px;font-weight:700;letter-spacing:0.5px;">1 USD = <span style="color:var(--accent);">SGD {fx["rate"]:.4f}</span></div>'
        f'<div class="muted" style="font-size:11px;margin-top:2px;">Source: <b>{fx["source"]}</b> · Last updated: <b>{fx["last_updated"]}</b></div>'
        '</div>'

        '<form method="post" action="/config/fx" style="margin-top:18px;">'
        '<label style="display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Source</label>'
        f'<select name="source" style="width:100%;padding:10px 12px;font-size:14px;background:#2c2c2e;color:var(--fg);border:1px solid var(--sep);border-radius:8px;margin-bottom:12px;letter-spacing:normal;text-align:left;">'
        f'{options}</select>'

        '<label style="display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Rate (used if Source = manual, or as fallback)</label>'
        f'<input type="text" name="rate" value="{fx["rate"]}" inputmode="decimal" pattern="[0-9.]*" style="letter-spacing:normal;text-align:left;">'

        '<div style="display:flex;gap:10px;margin-top:14px;">'
        '<button type="submit" name="action" value="save">Save</button>'
        '<button type="submit" name="action" value="fetch" class="btn-secondary" style="background:transparent;border:1px solid var(--accent);color:var(--accent);">Fetch from source</button>'
        '</div>'
        '</form>'

        '<p class="muted" style="margin-top:18px;font-size:11px;">'
        'Manual: just edit the rate. xe.com / OANDA: pick the source then click "Fetch from source" — the live rate is saved to the YAML.'
        '</p>'

        '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout("FX Rate", body)


async def render_connectors_page(user) -> str:
    name = user.telegram_username or f"id:{user.telegram_user_id}"
    rows = await conn_mod.check_all()
    by_group: dict = {}
    for r in rows:
        by_group.setdefault(r["group"], []).append(r)

    sections_html = ""
    for group in ["Data store", "Accounts", "Integrations", "Notifications", "Network", "Crypto"]:
        if group not in by_group: continue
        cards = ""
        for r in by_group[group]:
            ok = r.get("ok")
            if ok is True:
                status_ico, status_color = "🟢", "var(--accent)"
            elif ok is False:
                status_ico, status_color = "🔴", "var(--neg)"
            else:
                status_ico, status_color = "🟡", "#ffcc00"
            cards += (
                f'<div class="config-row" style="display:flex;flex-direction:column;align-items:stretch;gap:4px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
                f'<span class="name"><span class="ico">{r.get("icon","•")}</span> {r["name"]}</span>'
                f'<span style="font-size:14px;">{status_ico}</span></div>'
                f'<div class="sub" style="padding-left:24px;">{r.get("purpose","")}</div>'
                f'<div class="sub" style="padding-left:24px;color:{status_color};font-family:ui-monospace,monospace;font-size:10px;">'
                f'{r.get("detail","")}</div></div>'
            )
        sections_html += f'<div class="section-label">{group}</div>{cards}'

    onedrive_setup_html = (
        '<div class="config-row" style="display:block;background:rgba(76,217,100,0.05);border:1px dashed var(--accent);">'
        '<div class="name" style="margin-bottom:6px;"><span class="ico">📂</span> <b>OneDrive drop folder</b></div>'
        '<div class="sub">Drop bank/CC statements into <b>C:\\Users\\azfar\\OneDrive\\Sentinel Finance\\Statements\\auto-parse\\</b>. '
        'Future: Sentinel will scan this folder daily, parse PDFs/CSVs, and import to Firefly automatically.</div>'
        '<div class="sub" style="color:#ffcc00;margin-top:6px;">Status: folder convention defined · auto-parse not yet wired (planned P12)</div>'
        '</div>'
    )

    body = (
        '<a class="back" href="/config">&larr; Back</a>'
        '<h1>Connectors</h1>'
        f'<p class="meta">Signed in as <b>@{name}</b> · status refreshed live</p>'
        + sections_html
        + '<div class="section-label">Planned</div>'
        + onedrive_setup_html
        + '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout("Connectors", body)


def render_coming_soon(page: str) -> str:
    pretty = {
        "income": "Income Statement",
        "budget": "Budget",
        "forecast": "Cash Forecast",
        "gmail": "Gmail integration",
        "drive": "Google Drive integration",
        "telegram-bot": "Telegram bot integration",
        "fx": "FX rate settings",
        "glance": "Glance card customisation",
        "threshold": "Chain dust threshold",
    }.get(page or "", "This page")
    body = (
        '<a class="back" href="/">&larr; Back</a>'
        '<h1>Coming soon</h1>'
        f'<div class="todo-banner"><b>{pretty}</b> isn\'t wired up yet.<br>'
        'It\'s on the roadmap.</div>'
        '<footer>By Azfar · Powered by Claude</footer>'
    )
    return _layout("Coming soon", body)
