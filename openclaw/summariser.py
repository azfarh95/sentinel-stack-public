"""Phase 7 — rolling summariser.

Calls LM Studio (via the inference bridge at :8095) to compress a chunk
of conversation into a short summary that can stand in for the original
messages in the model's context.

Used by `brain_store.load_for_llm` when the token-budget would force
dropping ≥10 older messages: instead of evicting them, we synthesise
one `is_summary=TRUE` row that keeps the gist while costing ~200-400
tokens regardless of the original range size.

The summariser is **injectable** — callers can pass a `summariser` arg
to `BrainStore(...)` so tests don't need a live LM.  The default
implementation hits `http://127.0.0.1:8095/v1/chat/completions`.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Callable, Iterable

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None  # type: ignore


logger = logging.getLogger("openclaw.summariser")


DEFAULT_LM_URL = os.environ.get("BRAIN_SUMMARISER_LM_URL", "http://127.0.0.1:8095/v1/chat/completions")
DEFAULT_MODEL  = os.environ.get("BRAIN_SUMMARISER_MODEL", "qwen/qwen3.6-27b")
# Bumped 400→1500 after observing Qwen3's chain-of-thought consume 399/400
# tokens before producing any actual content (`finish_reason: length`,
# `reasoning_tokens: 399`). `/no_think` below tells Qwen3 to skip CoT.
DEFAULT_MAX_TOKENS = int(os.environ.get("BRAIN_SUMMARISER_MAX_TOKENS", "1500"))


SYSTEM_PROMPT = (
    "/no_think\n"
    "You are a conversation summariser embedded in a long-running assistant. "
    "You are given a chunk of older messages from one conversation thread. "
    "Compress them into ONE concise paragraph (3-6 sentences) that preserves: "
    "(a) named entities and facts the assistant must keep recalling, "
    "(b) decisions reached or commitments made, "
    "(c) open questions or unresolved threads. "
    "Drop pleasantries, restated context, and intermediate tool outputs. "
    "Do NOT add interpretation or commentary. "
    "Write in the third person (e.g. 'The user asked about X. Dove explained Y.'). "
    "Output only the summary paragraph — no preamble, no bullet points, no labels."
)


def _lm_api_key() -> str:
    """Read LM Studio API key from WCM (same entry the bridge uses)."""
    if not keyring:
        return ""
    try:
        return keyring.get_password("sentinel-watchdog", "lm_api_key") or ""
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("WCM read for lm_api_key failed: %s", exc)
        return ""


def _format_messages_for_summary(messages: Iterable[dict]) -> str:
    """Render a list of message dicts as a compact transcript for the summariser."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        # Truncate any single message at 1500 chars so the summariser input
        # stays bounded even on pathological inputs.
        if len(content) > 1500:
            content = content[:1500] + " […truncated]"
        # Tool/tool_result rows: keep a brief marker but skip the bulk
        if role == "tool":
            content = f"[tool result]: {content[:200]}"
        lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


def summarise_via_lm_studio(
    messages: list[dict],
    *,
    lm_url: str = DEFAULT_LM_URL,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = 90.0,
) -> str:
    """Default summariser — POSTs an OpenAI-shaped chat completion to the
    inference bridge.  Returns the summary text or raises on error.

    Per `feedback_openclaw_stalled_model_call`, total body size matters.
    We cap each input message at 1500 chars; with ≤200 input messages
    that's <300KB worst-case, comfortably under the 100KB stall threshold
    if we keep the chunk size at ~50 messages per summary call."""
    transcript = _format_messages_for_summary(messages)
    if not transcript:
        return ""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Summarise this conversation chunk per the system prompt:\n\n{transcript}"},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    key = _lm_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(lm_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        logger.error("summariser LM call HTTP %s: %s", exc.code, exc.read()[:500])
        raise
    except Exception as exc:
        logger.error("summariser LM call failed: %s", exc)
        raise

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"summariser got no choices: {data}")
    text = (choices[0].get("message") or {}).get("content") or ""
    return text.strip()


# Type alias for injectable summarisers
SummariserFn = Callable[[list[dict]], str]
