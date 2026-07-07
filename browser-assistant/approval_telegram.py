"""P4.2 — Telegram owner-in-the-loop approval channel for the browser agent.

`make_telegram_approver(token, chat_id, timeout_s) -> async approve(name, params)->bool`
sends the owner an inline ✓ Approve / ✗ Deny prompt for each state-changing action
and waits for the tap; timeout → deny. This is what makes a HEADLESS gated run
usable: the agent runs unattended and pings your phone before any click/type.

Reuses the testbot (`TESTBOT_TOKEN`, owner chat) — nothing else long-polls it, so
its getUpdates is free to consume here. Actions serialize (one approve() in flight
at a time), so there's never a concurrent getUpdates.
"""
import asyncio
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

_API = "https://api.telegram.org/bot{token}/{method}"


def _post(token, method, payload, timeout=20):
    req = urllib.request.Request(
        _API.format(token=token, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(token, method, params, timeout=35):
    url = _API.format(token=token, method=method) + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _short(params, n=180):
    try:
        return json.dumps(params, ensure_ascii=False)[:n]
    except Exception:
        return str(params)[:n]


def load_testbot_creds(env_path=None):
    """(token, chat_id) from metamcp-local/.env.local: TESTBOT_TOKEN + TESTBOT_CHAT_ID
    (chat falls back to the known owner chat YOUR_TELEGRAM_CHAT_ID)."""
    p = Path(env_path) if env_path else Path(__file__).resolve().parent.parent / ".env.local"
    env = ""
    try:
        env = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        pass
    m = re.search(r'^\s*TESTBOT_TOKEN\s*=\s*"?([^"\r\n]+)', env, re.M)
    token = m.group(1).strip() if m else ""
    mc = re.search(r'^\s*TESTBOT_CHAT_ID\s*=\s*"?([^"\r\n]+)', env, re.M)
    chat = (mc.group(1).strip() if mc else "YOUR_TELEGRAM_CHAT_ID")
    return token, chat


def _approve_sync(token, chat_id, name, params, timeout_s, page=None) -> bool:
    aid = f"ba{int(time.time() * 1000)}"
    where = f" on <b>{page}</b>" if page else ""
    text = (f"🌐 <b>Browser agent</b> wants to run:\n<b>{name}</b>{where}\n"
            f"<code>{_short(params)}</code>\n\nApprove?")
    kb = {"inline_keyboard": [[
        {"text": "✓ Approve", "callback_data": f"{aid}:y"},
        {"text": "✗ Deny", "callback_data": f"{aid}:n"},
    ]]}
    try:
        sent = _post(token, "sendMessage",
                     {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb})
        mid = sent.get("result", {}).get("message_id")
    except Exception:
        return False  # can't reach Telegram → fail closed

    deadline = time.time() + timeout_s
    offset = None
    while time.time() < deadline:
        try:
            gu = {"timeout": 25, "allowed_updates": json.dumps(["callback_query"])}
            if offset is not None:
                gu["offset"] = offset
            resp = _get(token, "getUpdates", gu)
        except Exception:
            time.sleep(2)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq or not str(cq.get("data", "")).startswith(aid + ":"):
                continue
            decision = cq["data"].endswith(":y")
            try:
                _post(token, "answerCallbackQuery",
                      {"callback_query_id": cq["id"], "text": "✓ approved" if decision else "✗ denied"})
            except Exception:
                pass
            try:
                if mid:
                    _post(token, "editMessageText",
                          {"chat_id": chat_id, "message_id": mid, "parse_mode": "HTML",
                           "text": text + ("\n\n→ ✅ APPROVED" if decision else "\n\n→ ⛔ DENIED")})
            except Exception:
                pass
            return decision
    # timed out → deny
    try:
        if mid:
            _post(token, "editMessageText",
                  {"chat_id": chat_id, "message_id": mid, "parse_mode": "HTML",
                   "text": text + "\n\n→ ⌛ timed out → DENIED"})
    except Exception:
        pass
    return False


def make_telegram_approver(token, chat_id, *, timeout_s=180):
    """Return an async approve(name, params)->bool that prompts the owner on Telegram."""
    async def approve(name, params, page=None) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _approve_sync, token, chat_id, name, params, timeout_s, page)
    return approve
