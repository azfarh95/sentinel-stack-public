"""Mini App view for the Chart of Accounts tree."""
from __future__ import annotations

from sqlalchemy import select

from . import database as db
from . import ledger


def build_tree() -> list[dict]:
    """Return CoA as a list of root nodes with nested children."""
    s = db.SessionLocal()
    try:
        rows = s.execute(
            select(ledger.ChartOfAccount).order_by(ledger.ChartOfAccount.account_code)
        ).scalars().all()
        nodes = {r.id: {"id": r.id, "code": r.account_code, "name": r.account_name,
                        "class": r.account_class, "subclass": r.account_subclass,
                        "normal_balance": r.normal_balance,
                        "is_postable": r.is_postable,
                        "is_control": r.is_control_account,
                        "sub_ledger": r.sub_ledger_table,
                        "parent_id": r.parent_id,
                        "children": []} for r in rows}
        roots = []
        for n in nodes.values():
            if n["parent_id"] is None:
                roots.append(n)
            elif n["parent_id"] in nodes:
                nodes[n["parent_id"]]["children"].append(n)
        return roots
    finally:
        s.close()


def _render_node(n: dict, depth: int = 0) -> str:
    pad = depth * 14
    icon = "📁" if not n["is_postable"] else ("⛓" if n["is_control"] else "·")
    sub = f' <span style="color:#8e8e93;font-size:11px;">[{n["sub_ledger"]}]</span>' if n["sub_ledger"] else ""
    klass_color = {
        "ASSET": "#4cd964", "LIABILITY": "#ff9500",
        "EQUITY": "#5ac8fa", "REVENUE": "#4cd964", "EXPENSE": "#ff3b30",
    }.get(n["class"], "#f0f0f0")
    weight = "600" if not n["is_postable"] else "400"
    line = (
        f'<div style="padding:4px 8px 4px {pad}px;font-weight:{weight};'
        f'border-left:3px solid {klass_color};margin:1px 0;font-size:13px;">'
        f'{icon} <code style="color:#8e8e93;font-size:11px;">{n["code"]}</code> '
        f'<span style="margin-left:6px;">{n["name"]}</span>{sub}</div>'
    )
    for child in n["children"]:
        line += _render_node(child, depth + 1)
    return line


def render_html() -> str:
    roots = build_tree()
    # Sort roots by class order
    class_order = ["ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"]
    roots.sort(key=lambda r: class_order.index(r["class"]) if r["class"] in class_order else 99)
    body_inner = "".join(_render_node(r) for r in roots)
    total = sum(_count(r) for r in roots)

    css = """
    body { background:#1c1c1e; color:#f0f0f0; font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;
           margin:0; padding:18px 14px 60px; max-width:1000px; margin:0 auto; }
    h1 { font-size:22px; margin:0 0 4px; }
    .meta { color:#8e8e93; font-size:12px; margin-bottom:18px; }
    .back { color:#4cd964; font-size:13px; text-decoration:none; display:inline-block; margin-bottom:10px; }
    .legend { background:#2c2c2e; border-radius:10px; padding:10px 14px; margin-bottom:14px;
              font-size:12px; color:#8e8e93; }
    .legend code { color:#f0f0f0; font-size:11px; }
    """
    body = f"""
    <a class="back" href="/">&larr; Home</a>
    <h1>Chart of Accounts</h1>
    <div class="meta">IAS 1-aligned · {total} accounts · seeded from <code>app/ledger_seed.py</code></div>
    <div class="legend">
      <b>📁</b> header (parent) ·
      <b>·</b> postable leaf ·
      <b>⛓</b> control account (has a sub-ledger like <code>credit_facilities</code> or <code>investment_positions</code>) ·
      colour stripe = class (green=asset/revenue · orange=liability · blue=equity · red=expense)
    </div>
    {body_inner}
    """
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Chart of Accounts — Sentinel Finance</title>'
        f'<style>{css}</style>'
        '</head><body>' + body + '</body></html>'
    )


def _count(n: dict) -> int:
    return 1 + sum(_count(c) for c in n["children"])
