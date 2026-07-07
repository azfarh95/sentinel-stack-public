"""
keyboard_bot.py — injects a persistent ReplyKeyboard into the Sentinel group on startup.

Buttons: /save-new  |  /new
         /memory-update

Also sends (and pins) a dashboard panel with a web_app button if mini_app_url is
configured in sentinel_config.json. Leave mini_app_url empty to skip the panel.

Tapping a keyboard button sends that text as the user's message in the group — OpenClaw
picks it up and acts on it. Stores message IDs so old messages are cleaned up on each run.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def _load_config() -> dict:
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


_cfg = _load_config()
BOT_TOKEN  = _cfg.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
_chat_ids  = _cfg.get("telegram_chat_ids", {})
CHAT_ID    = _chat_ids.get("group")
_dm_id     = _chat_ids.get("dm")
if not CHAT_ID or not _dm_id:
    raise SystemExit("config.json must define telegram_chat_ids.dm and telegram_chat_ids.group")
OWNER_ID   = int(_dm_id)

TEMP = os.environ.get("TEMP", os.path.dirname(__file__))
MSG_ID_FILE      = os.path.join(TEMP, "sentinel_keyboard_msg.id")
PANEL_ID_FILE    = os.path.join(TEMP, "sentinel_panel_msg.id")
DM_PANEL_ID_FILE = os.path.join(TEMP, "sentinel_dm_panel_msg.id")
import pathlib as _pathlib
CONFIG_FILE      = str(_pathlib.Path(__file__).resolve().parent.parent / "sentinel_config.json")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def api_call(method: str, **kwargs) -> dict:
    payload = json.dumps(kwargs).encode()
    req = urllib.request.Request(
        f"{API}/{method}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def get_mini_app_url() -> str:
    try:
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            return json.load(f).get("mini_app_url", "").strip()
    except Exception:
        return ""


def _delete_msg(id_file: str, label: str, chat_id=None) -> None:
    if not os.path.exists(id_file):
        return
    target = chat_id or CHAT_ID
    try:
        with open(id_file) as f:
            old_id = f.read().strip()
        if old_id:
            result = api_call("deleteMessage", chat_id=target, message_id=int(old_id))
            if result.get("ok"):
                print(f"Deleted previous {label} (id={old_id})")
        os.remove(id_file)
    except Exception as e:
        print(f"Note: could not delete old {label}: {e}")


def send_keyboard() -> None:
    result = api_call(
        "sendMessage",
        chat_id=CHAT_ID,
        text="🤖 Sentinel online",
        reply_markup={
            "keyboard": [
                [{"text": "/save-new"}, {"text": "/new"}],
                [{"text": "/memory-update"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        },
    )
    if result.get("ok"):
        msg_id = result["result"]["message_id"]
        with open(MSG_ID_FILE, "w") as f:
            f.write(str(msg_id))
        print(f"Keyboard sent (message_id={msg_id})")
    else:
        print(f"Failed to send keyboard: {result}", file=sys.stderr)
        sys.exit(1)


def send_panel(url: str) -> None:
    result = api_call(
        "sendMessage",
        chat_id=CHAT_ID,
        text="🖥️ *Sentinel Dashboard*",
        parse_mode="Markdown",
        reply_markup={
            "inline_keyboard": [[
                {"text": "📊 Open Dashboard", "url": url}
            ]]
        },
    )
    if not result.get("ok"):
        print(f"Failed to send panel: {result}", file=sys.stderr)
        return

    msg_id = result["result"]["message_id"]
    with open(PANEL_ID_FILE, "w") as f:
        f.write(str(msg_id))

    pin = api_call("pinChatMessage", chat_id=CHAT_ID, message_id=msg_id,
                   disable_notification=True)
    if pin.get("ok"):
        print(f"Dashboard panel pinned (message_id={msg_id})")
    else:
        print(f"Dashboard panel sent but not pinned: {pin.get('description','')}")


def send_dm_panel(direct_url: str) -> None:
    """Send web_app panel to owner DM — web_app buttons work in private chats."""
    result = api_call(
        "sendMessage",
        chat_id=OWNER_ID,
        text="🖥️ *Sentinel Dashboard*",
        parse_mode="Markdown",
        reply_markup={
            "inline_keyboard": [[
                {"text": "📊 Open Dashboard", "web_app": {"url": direct_url}}
            ]]
        },
    )
    if not result.get("ok"):
        print(f"Failed to send DM panel: {result}", file=sys.stderr)
        return

    msg_id = result["result"]["message_id"]
    with open(DM_PANEL_ID_FILE, "w") as f:
        f.write(str(msg_id))
    print(f"DM dashboard panel sent (message_id={msg_id})")


if __name__ == "__main__":
    _delete_msg(MSG_ID_FILE, "keyboard message")
    _delete_msg(PANEL_ID_FILE, "dashboard panel")
    _delete_msg(DM_PANEL_ID_FILE, "DM dashboard panel", chat_id=OWNER_ID)
    send_keyboard()
    url = get_mini_app_url()
    if url:
        send_panel(url)
        send_dm_panel("https://your-domain.example.com")
    else:
        print("mini_app_url not set in sentinel_config.json — skipping dashboard panel")
