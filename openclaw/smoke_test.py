"""OpenClaw gateway smoke test — the post-update gate.

Run this AFTER changing the gateway version (npm install -g openclaw@X) and
BEFORE re-enabling the live Telegram bot. It drives the *exact* CLI + JSON
contract the bot depends on — `openclaw_one_shot()` → `extract_reply()`.

Two check kinds, so we don't repeat the blind spot that let a broken bump look
green (it only used imperative prompts, which always answer):

  * must_answer  — a real, non-empty reply is REQUIRED (questions, content-
                   bearing greetings). An empty turn here is a FAIL — it would
                   only reach the user as the dispatcher's greeting fallback,
                   which is wrong for a real question.
  * fallback_ok  — empty is acceptable. A bare one-word greeting makes the
                   agent fire a tool-search over the 200+ tool surface and
                   finish with 0 payloads; the bot's dispatcher.py turns that
                   into a friendly fallback. This is version-INDEPENDENT
                   (same on 2026.5.28 and 2026.6.1), so it must not block a bump.

Usage (bot stopped first so turns don't race the gateway turnstile):

    cd C:\\Users\\azfar\\metamcp-local
    py -m openclaw.smoke_test

Exit 0 = every must_answer check passed (safe to re-enable / keep the bump).
Exit 1 = a must_answer check failed (roll back: npm install -g openclaw@<pin>
+ restore the config snapshot, then restart openclaw-gateway).
"""
from __future__ import annotations

import io
import sys
import time
import uuid

# Model replies (and our own markers) contain emoji / arrows; the Windows
# console default (cp1252) raises on them. Force UTF-8 so a print can't crash
# the gate and corrupt the exit code.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

from openclaw.brain_wrapper import extract_reply, openclaw_one_shot

# (name, message, kind, predicate-over-reply-text)
_CHECKS = [
    ("question",        "What is the capital of France? Answer in one word.",
     "must_answer", lambda t: "paris" in t.lower()),
    ("reasoning",       "What is 2 + 2? Answer with just the number.",
     "must_answer", lambda t: "4" in t),
    ("greeting_content", "hi there, how are you today?",
     "must_answer", lambda t: True),                       # any real reply
    ("greeting_bare",   "hello",
     "fallback_ok", None),                                  # empty acceptable
]

# A turn that comes back ok but reports a context_limit below this means the
# contextTokens autosync wrote the 32768 fallback (ADR INF-010) → the prompt-
# overflow "No response generated" path.
_MIN_CONTEXT_LIMIT = 60000


def run() -> int:
    print("OpenClaw gateway smoke test — driving the live brain_wrapper contract\n")
    must_fail = 0
    for name, msg, kind, check in _CHECKS:
        sid = str(uuid.uuid4())
        t0 = time.time()
        try:
            r = extract_reply(openclaw_one_shot(session_id=sid, message=msg, timeout_s=90))
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": "exception", "detail": str(e)}
        dt = int((time.time() - t0) * 1000)
        raw = (r.get("reply") or "").strip()

        if kind == "fallback_ok":
            ok = bool(r.get("ok"))
            verdict = "PASS" if ok else "FAIL"
            tail = "real reply" if raw else "empty → bot fallback (acceptable, version-independent)"
        else:  # must_answer
            ok = bool(r.get("ok")) and bool(raw) and (check is None or check(raw))
            if not ok:
                must_fail += 1
            verdict = "PASS" if ok else "FAIL"
            tail = "real reply" if raw else "EMPTY — would hit fallback (bad for a real turn)"

        print(f"[{verdict}] {name:16} {kind:12} {dt:>6}ms  ok={r.get('ok')} "
              f"ctx={r.get('context_limit')}")
        print(f"           reply={raw[:80]!r}  ({tail})")
        if not ok and kind == "must_answer":
            print(f"           error={r.get('error')} detail={(r.get('detail') or '')[:160]}")
        cl = r.get("context_limit")
        if r.get("ok") and isinstance(cl, int) and cl < _MIN_CONTEXT_LIMIT:
            print(f"           ⚠ context_limit {cl} < {_MIN_CONTEXT_LIMIT} — autosync may have written the fallback")

    print(f"\nmust_answer failures: {must_fail} — "
          f"{'SAFE — keep/enable this gateway' if must_fail == 0 else 'NOT SAFE — roll back'}")
    return 0 if must_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
