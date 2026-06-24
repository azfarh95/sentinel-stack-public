"""P4.2 live round-trip: send a real approval prompt to the owner's testbot and
wait for the ✓/✗ tap. Confirms sendMessage + inline keyboard + getUpdates callback
+ answerCallbackQuery + editMessageText + the returned decision."""
import asyncio

from approval_telegram import _post, load_testbot_creds, make_telegram_approver

token, chat = load_testbot_creds()
assert token, "no TESTBOT_TOKEN in .env.local"

_post(token, "sendMessage", {
    "chat_id": chat,
    "text": "🔔 P4.2 channel test — a browser-approval prompt is next. "
            "Tap ✓ Approve or ✗ Deny to confirm the channel works."})

approve = make_telegram_approver(token, chat, timeout_s=150)
ok = asyncio.run(approve("click", {"target": "demo button", "note": "P4.2 live test"}))
print("DECISION:", "APPROVED" if ok else "DENIED/timeout")  # ASCII — cp1252 console safe
