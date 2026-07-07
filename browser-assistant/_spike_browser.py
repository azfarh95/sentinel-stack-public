"""B nucleus spike — does LOCAL Qwen (27B) drive browser-use through a tier-1
read/extract task? Make-or-break for the whole browser-assistant track.

Contained: a throwaway HEADLESS Chrome (separate profile, no touch to the real
Comet), LLM = local Qwen via infer-bridge :8095 (OpenAI-compatible). Measures
whether the local model reliably produces the structured actions browser-use
needs, on a simple, stable page (a real tier-1: navigate + extract)."""
import asyncio
import os
import sys
import time

from browser_use import Agent, Browser, ChatOpenAI

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "_chrome_profile")     # throwaway, separate from real browser
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

TASK = ("Go to https://example.com and report the exact text of the page's main "
        "heading (the <h1>). Answer with just that heading text.")


async def main():
    llm = ChatOpenAI(
        model="qwen/qwen3.6-27b",
        base_url="http://127.0.0.1:8095/v1",
        api_key="local",                 # :8095 is loopback; key is a placeholder
        temperature=0.0,
        # local 27B has no reliable native function-calling / structured output —
        # put the action schema in the prompt and parse text instead.
        add_schema_to_system_prompt=True,
    )
    browser = Browser(
        executable_path=CHROME if os.path.exists(CHROME) else None,
        user_data_dir=PROFILE,
        headless=True,
    )
    agent = Agent(task=TASK, llm=llm, browser=browser)

    print(f"[spike] task: {TASK}")
    t0 = time.time()
    try:
        history = await agent.run(max_steps=8)
        dt = time.time() - t0
        # final result extraction (API-tolerant)
        final = None
        for attr in ("final_result", "final_answer"):
            fn = getattr(history, attr, None)
            if callable(fn):
                try:
                    final = fn()
                except Exception:
                    pass
            if final:
                break
        steps = None
        for attr in ("number_of_steps", "n_steps"):
            fn = getattr(history, attr, None)
            if callable(fn):
                try:
                    steps = fn()
                except Exception:
                    pass
        print(f"\n[spike] DONE in {dt:.0f}s  steps={steps}")
        print(f"[spike] final result: {final!r}")
        ok = final and "example domain" in str(final).lower()
        print(f"[spike] VERDICT: {'PASS — local Qwen drove the read/extract task' if ok else 'INCONCLUSIVE — check the result/log above'}")
    except Exception as e:
        dt = time.time() - t0
        print(f"\n[spike] FAILED after {dt:.0f}s: {type(e).__name__}: {e}")
        raise
    finally:
        try:
            await browser.kill()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
