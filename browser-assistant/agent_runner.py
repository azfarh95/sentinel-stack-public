"""Track B — P0: FENCED browser-use runner (the 3.3 turn-fence analog).

Wraps browser-use with a step-cap + a wall-clock fence so a runaway/stuck task
self-terminates and frees the :8095 inference slot instead of wedging it. LLM
calls route through infer-bridge :8095 (inherits the broker FIFO queue + the 2.3
fast-503, so a wedged backend fails a step fast, not the whole task). Throwaway
HEADLESS Chrome (separate profile) — never touches the real Comet. Structured
JSONL run log for the P1 capability suite.
"""
import asyncio
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# Windows console is cp1252; agent output contains emoji/unicode. Make every script
# that imports this module print safely instead of crashing on UnicodeEncodeError
# (cf. memory reference_powershell51_ascii_scheduled_task). runs.jsonl is already utf-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from browser_use import Agent, Browser, ChatOpenAI

HERE = os.path.dirname(os.path.abspath(__file__))
RUNLOG = os.path.join(HERE, "runs.jsonl")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BRIDGE = "http://127.0.0.1:8095"


def _now():
    return datetime.now(timezone.utc).isoformat()


def log(rec: dict):
    rec["ts"] = _now()
    with open(RUNLOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def bridge_active() -> str:
    """infer-bridge active-count + backend state — to confirm the fence frees the slot."""
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=4) as r:
            d = json.loads(r.read().decode())
        return f"active={d.get('active')} backend={d.get('backend')} blocked={d.get('blocked')}"
    except Exception as e:
        return f"health-err:{type(e).__name__}"


def _llm():
    # local Qwen via :8095; schema-in-prompt is essential for a local model.
    return ChatOpenAI(model="qwen/qwen3.6-27b", base_url=BRIDGE + "/v1",
                      api_key="local", temperature=0.0, add_schema_to_system_prompt=True)


async def run_task(task: str, *, label: str = "", caller: str = "", max_steps: int = 12,
                   max_wall_s: float = 240.0, use_vision: bool = False,
                   cdp_url: str | None = None, approve=None, persist: bool = False,
                   on_step=None) -> dict:
    """Run one browser task under the fence. Returns a structured result record.

    cdp_url set → ATTACH to an already-running browser (the real Comet, launched
    with --remote-debugging-port); else a throwaway HEADLESS Chrome (isolated).
    approve set → use GatedTools: state-changing actions (click/type/submit/...)
    require approve(name, params)->bool first (P4 approval gate); reads pass through.
    on_step set → a sync callback(agent) fired after each step (live panel progress)."""
    t0 = time.time()
    if cdp_url:
        browser = Browser(cdp_url=cdp_url)
    else:
        browser = Browser(executable_path=CHROME if os.path.exists(CHROME) else None, headless=True)
    agent_kw = dict(task=task, llm=_llm(), browser=browser, use_vision=use_vision)
    if approve is not None:
        from tools_gated import GatedTools
        agent_kw["tools"] = GatedTools(approve=approve)
    agent = Agent(**agent_kw)
    status, final, steps, err = "unknown", None, None, None
    on_step_end = None
    if on_step is not None:
        async def on_step_end(ag):
            try:
                on_step(ag)
            except Exception:
                pass
    try:
        # THE FENCE: bound the whole agent loop by wall-clock; on timeout cancel + clean up.
        history = await asyncio.wait_for(
            agent.run(max_steps=max_steps, on_step_end=on_step_end), timeout=max_wall_s)
        try:
            final = history.final_result()
        except Exception:
            final = None
        for attr in ("number_of_steps", "n_steps"):
            fn = getattr(history, attr, None)
            if callable(fn):
                try:
                    steps = fn(); break
                except Exception:
                    pass
        status = "ok"
    except asyncio.TimeoutError:
        status, err = "fenced_timeout", f"wall fence {max_wall_s}s exceeded"
    except Exception as e:
        status, err = "error", f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        # never kill an ATTACHED real browser (Comet) — only the throwaway headless one
        if not cdp_url:
            try:
                await browser.kill()
            except Exception:
                pass
    rec = {"label": label, "caller": (caller or label or "api"), "task": task[:140],
           "status": status, "steps": steps, "dur_s": round(time.time() - t0, 1),
           "final": (str(final)[:240] if final is not None else None),
           "err": err, "use_vision": use_vision, "max_wall_s": max_wall_s,
           "gated": approve is not None, "model": "qwen/qwen3.6-27b"}
    log(rec)
    if persist:
        try:
            from persist import persist_turn
            persist_turn({**rec, "task": task})   # full task into the surface log + brain
        except Exception:
            pass
    return rec


async def _smoke():
    print("baseline bridge:", bridge_active())
    # 1) normal task → expect ok
    r1 = await run_task("Go to https://example.com and report the exact text of the main heading (the <h1>).",
                        label="p0-normal", max_steps=6, max_wall_s=120)
    print("normal:", r1["status"], "steps", r1["steps"], "dur", r1["dur_s"], "final", r1["final"])
    print("post-normal bridge:", bridge_active())
    # 2) multi-step task with a TINY wall fence → expect fenced_timeout, slot freed
    r2 = await run_task("Visit https://example.com, then https://en.wikipedia.org, then https://example.org, "
                        "and write a one-paragraph summary of each.",
                        label="p0-fence", max_steps=20, max_wall_s=25)
    print("fence:", r2["status"], "dur", r2["dur_s"], "err", r2["err"])
    # give the cancelled in-flight call a moment to drop, then confirm the slot freed
    await asyncio.sleep(5)
    print("post-fence bridge (active should be 0):", bridge_active())


if __name__ == "__main__":
    asyncio.run(_smoke())
