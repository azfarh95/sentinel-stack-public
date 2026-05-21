"""/config/imports — history page for CSV/PDF auto-imports.

Reads from db.ImportLog (last 30 rows, newest first). Shows per-file:
file name, account, n_rows, created/dup/err counts, ledger balance,
firefly balance, variance, trigger (manual / hourly_watcher), timestamp.

Surface for Task #28 in the v1.5 autopilot batch.
"""
from __future__ import annotations

from datetime import datetime

from . import database as db
from . import settings as app_settings


def load_history(limit: int = 30) -> list[dict]:
    s = db.SessionLocal()
    try:
        rows = (s.query(db.ImportLog)
                 .order_by(db.ImportLog.started_at.desc())
                 .limit(limit).all())
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else "",
                "source": r.source,
                "file_name": r.file_name,
                "account_id": r.account_id,
                "account_name": r.account_name,
                "n_rows": r.n_rows or 0,
                "created": r.created or 0,
                "duplicates": r.duplicates or 0,
                "errored": r.errored or 0,
                "ledger_balance": r.ledger_balance,
                "firefly_balance": r.firefly_balance,
                "variance": r.variance,
                "error_summary": r.error_summary,
                "moved_to": r.moved_to,
                "triggered_by": r.triggered_by,
            })
        return out
    finally:
        s.close()


def render_imports_page(user, flash: str = "") -> str:
    name = user.telegram_username or f"id:{user.telegram_user_id}"
    history = load_history(limit=30)

    flash_html = (
        f'<div style="background:rgba(76,217,100,0.10);border:1px solid var(--accent);'
        f'border-radius:8px;padding:10px;color:var(--accent);margin:12px 0;font-size:12px;">{flash}</div>'
    ) if flash else ""

    if not history:
        rows_html = (
            '<p class="meta" style="text-align:center;padding:20px;">'
            'No imports recorded yet. Drop a POSB iBanking CSV into '
            '<b>Sentinel Finance/Auto-import/POSB/</b> and click '
            '<b>Connectors → Scan &amp; import now</b>, or wait for the '
            'hourly watcher.</p>'
        )
    else:
        rows: list[str] = []
        for r in history:
            ts = ""
            try:
                ts = app_settings.format_date(r["started_at"][:10])
                ts += " " + r["started_at"][11:16]
            except Exception:
                ts = r["started_at"]
            var = r["variance"]
            var_cls = ""
            var_html = ""
            if var is not None:
                var_cls = "muted" if abs(var) < 0.50 else "neg"
                var_html = f'<span class="amt {var_cls}">var {var:+,.2f}</span>'
            err_html = ""
            if r["errored"]:
                err_html = (
                    f'<div class="muted" style="color:var(--neg);font-size:10px;'
                    f'padding-top:2px;">{(r["error_summary"] or "errors")[:120]}</div>'
                )
            counts_html = (
                f'<span class="pos">+{r["created"]}</span> · '
                f'<span class="muted">{r["duplicates"]} dup</span>'
            )
            if r["errored"]:
                counts_html += f' · <span class="neg">{r["errored"]} err</span>'
            rows.append(
                f'<div class="card" style="margin-top:8px;padding:12px 14px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
                f'<span class="name" style="font-weight:600;font-size:13px;">'
                f'{r["file_name"]}</span>'
                f'{var_html}'
                f'</div>'
                f'<div class="muted" style="font-size:11px;margin-top:2px;">'
                f'{ts} · {r["account_name"] or "—"} · {r["triggered_by"]}</div>'
                f'<div style="font-size:12px;margin-top:6px;">{counts_html}'
                f' of <b>{r["n_rows"]}</b> rows</div>'
                + err_html +
                '</div>'
            )
        rows_html = "\n".join(rows)

    body = (
        '<a class="back" href="/config">&larr; Back</a>'
        '<h1>Import history</h1>'
        f'<p class="meta">Signed in as <b>@{name}</b> · last {len(history)} imports</p>'
        + flash_html +
        '<form method="post" action="/config/connectors/import-csv" style="margin-bottom:8px;">'
        '<button type="submit" style="background:var(--accent);color:#000;border:none;'
        'padding:8px 14px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;">'
        'Scan &amp; import now</button>'
        '</form>'
        + rows_html +
        '<footer>By Azfar · Powered by Claude · Hourly watcher runs at :00 past every hour</footer>'
    )

    # Reuse home._layout shell
    from . import home as home_mod
    return home_mod._layout("Import history", body)
