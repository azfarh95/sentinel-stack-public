# Inference-stack consolidation review (2026-06-15)

Triggered by the owner's question: *"Are we complicating the infrastructure by
having multiple helpers — infer-bridge, llama-swap, GPU broker?"* — after a
multi-hour incident whose root cause was a **coordination blind spot** (ComfyUI
idle-squatting the GPU, spilling Qwen's KV cache → ~18× slower prefill → turn
wedges). Grounded in a full read of the live code, not assumptions.

## The chain today

```
OpenClaw (WSL) ──HTTP──► infer-bridge :8095 ──► llama-swap :1234 ──► llama-server (engine)
                              ▲                       ▲
                  GPU broker :8200 (watchdog) ────────┘   (also ──► ComfyUI :8821 /free, /system_stats)
```

## What each layer ACTUALLY does (live vs dead)

| Layer | Live responsibilities | Dead / vestigial | Verdict |
|---|---|---|---|
| **llama-server** | the inference engine | — | essential |
| **llama-swap :1234** | on-demand load, TTL idle-unload, `/running`, `/api/models/unload` | — | justified **only because the GPU is shared** — it's the evict/reload mechanism |
| **infer-bridge :8095** | `_blocked` gate + kill in-flight conns (`/infer_block`/`/infer_unblock`); FIFO queue-wait → broker; `/infer_status` (crib polls); `/health` decoupled from model-resident (fixed a restart storm); JSONL audit; transparent proxy | **classifier + 3-way model routing + `_resolve_target` fallback + model-field rewrite** (~150 lines) — all no-op since all three model classes are pinned to `qwen/qwen3.6-27b`. Docstring still says "LM Studio swaps models" (stale). | keep the gate/queue/status/proxy; **delete the dead routing** |
| **GPU broker :8200** (watchdog) | policy authority: presets, FLUX leases, gaming preempt, queue dispatch, Qwen block/evict actuation | VRAM *sensing* via ComfyUI `/system_stats` is **broken** (reports torch-local free, not system-wide — read 22 GB free while ComfyUI held 4.46) | keep, but **fix the truth source** |

## The real problem is NOT the process count

The 4 processes map to 4 genuinely distinct concerns (engine / load-lifecycle /
data-path gate / cross-consumer policy) with **different lifecycles** — infer-bridge
is a thin always-up proxy; the broker is watchdog policy with a probe loop, audit,
notify. Collapsing them couples things that should fail independently. The count is
roughly earned.

Two things are genuinely wrong:

1. **Dead weight in infer-bridge.** The multi-model classifier/router/fallback is
   ~150 lines of no-op that runs on every request and makes the layer *look* like
   it does much more than it does. This is the bulk of the "too many moving parts"
   feeling in that layer. Pure win to delete (no behavior change — single model).

2. **Policy and ground-truth are split, and the broker can't see the card.** The
   broker *decides* who owns the GPU but *infers* GPU state from HTTP to llama-swap
   + ComfyUI, and ComfyUI's sensor lies. That split is the blind spot that caused
   the incident. The fix is to make **one** thing authoritative on real VRAM, or to
   remove the need for the broker to sense VRAM at all by killing the squat at the
   source.

## Target (ordered by ROI — smallest, highest-impact first)

**1. Kill the ComfyUI squat at the source (reliability — the actual bug).**
   ComfyUI keeps its model resident after a render → squats Qwen. Make it not:
   either launch ComfyUI with `--cache-none` (unload after each prompt) **or** have
   the FLUX callers/broker `/free` ComfyUI on render completion. This removes the
   *need* for the broker to police idle residency (no poll thread, no perf-counter
   sensing). Trade: repeated FLUX renders reload (~17 s) — correct, since Qwen is
   the always-on brain and FLUX is occasional ("one chair, one model", AI-005).
   *Covers every FLUX path (leased and the un-leased arcade-forge/media-ai blind
   spots) because it's at the ComfyUI process.*

**2. Delete infer-bridge's dead routing (simplification — no behavior change).**
   Remove `_classify`, `_COMPLEX/TOOL/CODING/SIMPLE_KEYWORDS`, `_resolve_target`,
   the fallback chain, and the model-field rewrite. Keep `_get_loaded_models` only
   for `/infer_status`'s `loaded` field. Rewrite the docstring (it's llama-swap +
   one model, not "LM Studio swaps models"). ~150 lines out; the layer's true job
   (gate + queue + status + audit + proxy) becomes legible.

**3. Make the broker's VRAM truth reliable OR retire it.** If #1 lands, the broker
   no longer needs to *detect* a squatter — its lease/evict for *active* renders is
   enough, and the broken `_free_vram_gb()` preflight can be simplified. If we want
   belt-and-suspenders, replace the ComfyUI `/system_stats` read with the **Windows
   GPU perf counter** (`\GPU Process Memory\Dedicated Usage` per pid — the reliable
   signal that correctly showed the squat). Decide after #1.

**4. Doc/memory truth-up.** Correct the stale claims surfaced today:
   - The `q8kv` memory (`reference_qwen_q8kv_prefill_regression_rocm`) is **false** —
     KV quant is speed-neutral (clean A/B: q8_0 ≈ f16 ≈ 40 tok/s contended, ~736
     uncontended). Real cause = GPU contention. Rewrite/delete it.
   - AI-002/003/005 + the inference runbook: note the real prefill lever is GPU
     exclusivity, not KV format; the "OpenRouter fallback" line is stale (no such
     code).

## What NOT to do

- Don't merge processes — distinct lifecycles; the count isn't the problem.
- Don't add a broker poll/perf-counter/self-heal subsystem **if #1 makes it
  unnecessary** (it was over-engineering a fix for a problem better killed at source).
- Don't touch the workspace `.md` files further (lobotomy risk; owner-flagged).

## Open decision for the owner

Pick the scope: **(A)** just #1 (kill the squat) + #4 (docs); **(B)** #1 + #2
(also strip dead code); **(C)** all four including the belt-and-suspenders broker
sensor (#3). Recommendation: **B** — fixes the bug at the source and removes the
biggest chunk of real complexity, without bolting new machinery onto the broker.
