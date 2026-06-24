"""Token counting for the shared brain.

LM Studio's loaded model (qwen/qwen3.6-27b) is a Qwen-family BPE that has
no first-party Python tokenizer. We use `cl100k_base` (GPT-4 family) as a
rough proxy — published ratios put it within ~10-15% of true Qwen token
counts, which is fine for context-budget decisions. Phase 7 (context-
window management) can swap in a model-native tokenizer if budget
precision matters.

`chars // 4` is the lazy fallback when tiktoken isn't installed (matches
the heuristic the inference bridge already uses).
"""
from __future__ import annotations

import os

_ENCODER = None
_DISABLED = bool(os.environ.get("BRAIN_TOKENIZER_DISABLE"))


def _encoder():
    global _ENCODER
    if _DISABLED:
        return None
    if _ENCODER is not None:
        return _ENCODER
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODER = None
    return _ENCODER


def count_tokens(text: str) -> int:
    """Return an integer token estimate for `text`. Never raises.
    Returns at least 1 for non-empty strings so single-char messages
    still count toward the budget."""
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        return max(1, len(text) // 4)
    try:
        return max(1, len(enc.encode(text, disallowed_special=())))
    except Exception:
        return max(1, len(text) // 4)
