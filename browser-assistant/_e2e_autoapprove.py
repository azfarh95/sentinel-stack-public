"""E2E: gated run with an AUTO-APPROVE channel — confirms GatedTools + Agent
completes end-to-end when state-changes are approved (vs the console-deny path)."""
import asyncio
import json

from agent_runner import run_task


async def yes(name, params):
    print(f"[auto-approve] {name}", flush=True)
    return True


rec = asyncio.run(run_task(
    "Go to https://example.com and report the exact text of the main heading (the h1).",
    label="e2e-autoapprove", max_steps=6, max_wall_s=420, approve=yes))
print(json.dumps(rec, indent=2))
