"""Stealth browser backend (ADR MED-012 → browser-assistant 'stealth' mode).

A drop-in alternative to agent_runner.run_task that drives the **camofox** REST
service (anti-detection Firefox) instead of browser-use's headless Chrome — for
targets that block automation or need the clean proxied IP. browser-use 0.13 is
Chromium/CDP-only, so stealth mode is its own inner loop:

    create session → [snapshot → Qwen(goal+history+snapshot) → action → observe] → done

Reuses the assistant's existing machinery: the wall-clock fence, runs.jsonl `log`,
`persist`, the `domain_guard` policy, the `approve(name, params, page)` contract,
and the local Qwen at infer-bridge :8095. State-changing actions (click/type/
navigate-to-sensitive) gate through `approve`; reads pass. Throwaway camofox
`userId` per task — no bleed into the IG or brains' sessions.

Phase-0 spike (`_spike_camofox.py`) proved stealth (navigator.webdriver=false,
sannysoft "WebDriver: passed") + mechanics; this adds the action-history +
post-action observation the spike's lean loop lacked.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from domain_guard import DomainPolicy, domain_of

# Local runs.jsonl logger — same file/shape as agent_runner.log, but defined here so
# stealth mode does NOT import agent_runner (which pulls in browser-use). Keeps the
# stealth path independent of the Chromium stack it's meant to be an alternative to.
_RUNLOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.jsonl")


def log(rec: dict) -> None:
    rec["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(_RUNLOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass

CAMOFOX = os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377").rstrip("/")
BRIDGE  = "http://127.0.0.1:8095"
_ENVF   = r"C:\Users\azfar\metamcp-local\.env.local"


def _access_key() -> str:
    k = os.environ.get("CAMOFOX_ACCESS_KEY", "").strip()
    if k:
        return k
    try:
        for line in open(_ENVF, encoding="utf-8", errors="replace"):
            if line.startswith("CAMOFOX_ACCESS_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


_KEY = _access_key()


# ── transport (sync urllib, run off-thread so the event loop never blocks) ──────
def _cf_sync(method: str, path: str, body=None, q=None):
    url = CAMOFOX + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_KEY}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


async def _cf(method, path, body=None, q=None):
    return await asyncio.to_thread(_cf_sync, method, path, body, q)


def _qwen_sync(messages) -> str:
    body = {"model": "qwen/qwen3.6-27b", "messages": messages, "temperature": 0.0}
    req = urllib.request.Request(
        BRIDGE + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"]


async def _qwen(messages):
    return await asyncio.to_thread(_qwen_sync, messages)


_SYS = (
    "You drive a real web browser to accomplish a GOAL. Each turn you get the current URL, "
    "the STEPS you have already taken (with their results), and a SNAPSHOT of the page "
    "(an accessibility tree; interactive elements end with a ref like [e5]). Choose ONE next "
    "action and reply with ONLY a JSON object — no prose, no markdown:\n"
    '  {"action":"click","ref":"e5"}\n'
    '  {"action":"type","ref":"e5","text":"hello","submit":true}\n'
    '  {"action":"navigate","url":"https://..."}\n'
    '  {"action":"done","answer":"<final answer to the goal>"}\n'
    "Rules: use the ref EXACTLY as shown (e.g. e5). Do NOT repeat an action the STEPS list "
    "shows you already did — read its result and move on. Reply 'done' as soon as the "
    "snapshot already contains the answer.")


def _ref(act) -> str:           # tolerate e5 / [e5] / ref=e5
    return re.sub(r"[^a-z0-9]", "", str(act.get("ref", "")).lower())


def _parse(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def _ask(approve, name, params, page=None) -> bool:
    """Honor the same approval contract as GatedTools. Fail-closed on error."""
    if approve is None:
        return True
    try:
        return bool(await approve(name, params, page=page))
    except Exception:
        return False


async def run_stealth_task(task: str, *, label: str = "", caller: str = "", max_steps: int = 12,
                           max_wall_s: float = 240.0, use_vision: bool = False,
                           cdp_url: str | None = None, approve=None, persist: bool = False,
                           on_step=None) -> dict:
    """Stealth-mode sibling of agent_runner.run_task. Same kwargs (cdp_url/use_vision
    ignored — camofox is its own engine) and same result-record shape, so the :8108
    surface can dispatch to it by `mode=="stealth"` with no call-site change."""
    t0 = time.time()
    uid = re.sub(r"[^a-zA-Z0-9_-]", "", f"stealth-{int(t0)}-{label or 'task'}")[:40]
    policy = DomainPolicy.load()
    history: list[str] = []
    typed: set = set()   # (ref, text) already entered — the a11y snapshot doesn't show a
                         # textbox's value, so guard against re-typing the same field forever
    status, final, steps, err = "unknown", None, 0, None

    async def _loop():
        nonlocal final, steps
        # No initial URL (camofox 400s on about:blank); the agent navigates from the goal.
        tab = (await _cf("POST", "/tabs", {"userId": uid, "sessionKey": "s"}))["tabId"]
        try:
            for i in range(max_steps):
                steps = i + 1
                snap = await _cf("GET", f"/tabs/{tab}/snapshot", q={"userId": uid})
                url = snap.get("url") or ""
                page_txt = (snap.get("snapshot") or "")[:4000]
                hist = "\n".join(history[-10:]) or "(none yet)"
                raw = await _qwen([
                    {"role": "system", "content": _SYS},
                    {"role": "user", "content": f"GOAL: {task}\nURL: {url}\nSTEPS SO FAR:\n{hist}\n\nSNAPSHOT:\n{page_txt}"}])
                act = _parse(raw)
                if on_step is not None:
                    try:
                        on_step({"step": steps, "url": url, "action": act})
                    except Exception:
                        pass
                if not act:
                    history.append(f"step{steps}: model gave no valid action")
                    continue
                a = act.get("action")
                if a == "done":
                    final = act.get("answer")
                    return
                if a == "navigate":
                    tgt = act.get("url", "")
                    klass = policy.classify(tgt)
                    if klass == "blocked":
                        history.append(f"step{steps}: navigate {tgt} BLOCKED by domain policy")
                        continue
                    if klass == "sensitive" and not await _ask(approve, "navigate", {"url": tgt}, page=domain_of(tgt)):
                        history.append(f"step{steps}: navigate {tgt} DENIED by owner")
                        continue
                    await _cf("POST", f"/tabs/{tab}/navigate", {"userId": uid, "url": tgt})
                    new = (await _cf("POST", f"/tabs/{tab}/evaluate", {"userId": uid, "expression": "location.href"})).get("result")
                    history.append(f"step{steps}: navigated → {new}")
                elif a == "click":
                    ref = _ref(act)
                    if not await _ask(approve, "click", {"ref": ref, "url": url}, page=url):
                        history.append(f"step{steps}: click {ref} DENIED by owner")
                        continue
                    before = url
                    await _cf("POST", f"/tabs/{tab}/click", {"userId": uid, "ref": ref})
                    after = (await _cf("POST", f"/tabs/{tab}/evaluate", {"userId": uid, "expression": "location.href"})).get("result")
                    history.append(f"step{steps}: clicked {ref} → "
                                   + (f"navigated to {after}" if after != before else f"same page ({after})"))
                elif a == "type":
                    ref = _ref(act)
                    text = act.get("text", "")
                    if (ref, text) in typed:
                        history.append(f"step{steps}: field {ref} ALREADY contains '{text}' — do NOT type it "
                                       "again; fill the OTHER field, or click the submit/Login button, or 'done'.")
                        continue
                    if not await _ask(approve, "type", {"ref": ref, "text": text, "url": url}, page=url):
                        history.append(f"step{steps}: type into {ref} DENIED by owner")
                        continue
                    submit = bool(act.get("submit"))
                    await _cf("POST", f"/tabs/{tab}/type",
                              {"userId": uid, "ref": ref, "text": text, "submit": submit})
                    typed.add((ref, text))
                    history.append(f"step{steps}: typed '{text}' into {ref}" + (" and submitted" if submit else ""))
                else:
                    history.append(f"step{steps}: unknown action {a!r}")
                await asyncio.sleep(1.0)
        finally:
            try:
                await _cf("DELETE", f"/sessions/{uid}")
            except Exception:
                pass

    try:
        await asyncio.wait_for(_loop(), timeout=max_wall_s)
        status = "ok"
    except asyncio.TimeoutError:
        status, err = "fenced_timeout", f"wall fence {max_wall_s}s exceeded"
    except Exception as e:  # noqa: BLE001
        status, err = "error", f"{type(e).__name__}: {str(e)[:200]}"

    rec = {"label": label, "caller": (caller or label or "api"), "task": task[:140],
           "status": status, "steps": steps, "dur_s": round(time.time() - t0, 1),
           "final": (str(final)[:240] if final is not None else None),
           "err": err, "use_vision": False, "max_wall_s": max_wall_s,
           "gated": approve is not None, "model": "qwen/qwen3.6-27b", "mode": "stealth"}
    log(rec)
    if persist:
        try:
            from persist import persist_turn
            persist_turn({**rec, "task": task})
        except Exception:
            pass
    return rec


# ── standalone smoke (mirrors the spike, now with history/observation) ──────────
async def _smoke():
    print("CAMOFOX:", CAMOFOX, "| key:", bool(_KEY))
    r1 = await run_stealth_task(
        "What is the author of the very FIRST quote on https://quotes.toscrape.com/ ? Navigate there, answer just the name.",
        label="smoke-read", max_steps=5, max_wall_s=120)
    print("read:", r1["status"], "| steps", r1["steps"], "| final:", r1["final"])
    r2 = await run_stealth_task(
        "On https://quotes.toscrape.com/ click the 'Login' link, then report the final URL.",
        label="smoke-click", max_steps=6, max_wall_s=150)
    print("click:", r2["status"], "| steps", r2["steps"], "| final:", r2["final"])


if __name__ == "__main__":
    asyncio.run(_smoke())
