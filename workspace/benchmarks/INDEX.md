# Sentinel Stack — Agent Benchmark Database

Track every meaningful agent run (research, code, automation, etc.) so you can compare improvements over time and decide whether to keep tuning vs ship.

---

## Files in this directory

| File | Role | Maintenance |
|---|---|---|
| `INDEX.md` | This file. Latest-first summary + how to add new entries. | Hand-edited as benchmarks land. |
| `benchmarks.yaml` | Structured database, one entry per agent run. | Auto-extracted via `scripts/extract_benchmark.py`; hand-edit `quality_subjective_1to10` and `notes`. |

---

## Quick-add a new benchmark

After any non-trivial agent task:

```powershell
python scripts\extract_benchmark.py <trajectory.jsonl> `
    --from <first_turn> --to <last_turn> `
    --task "what you asked" `
    --output workspace\research\<artifact>.md `
    --agent sentinel-local --model qwen/qwen3.6-27b
```

Paste the YAML output into `benchmarks.yaml`. Fill in `quality_subjective_1to10` (1-10) and `notes` after reviewing the output.

For non-Sentinel agents (Claude general-purpose, etc.), there's no trajectory file — extract manually from the task notification.

---

## Latest benchmarks

### 2026-05-09 — V3 handheld-AI wishlist research

| Metric | Sentinel (local) | Claude (cloud, expansion pass) |
|---|---|---|
| **Wall-clock** | 10.0 min | 5.2 min |
| **Tokens (in/out/total)** | 451K / 6K / 457K | (only total: 62K) |
| **Tool calls** | 10 web_fetch + 5 search + 1 write | 20 total |
| **Output pages** (250 wpp) | **9 pages** | **18 pages** |
| **Output words** | 2,136 | 4,519 |
| **Energy (electricity)** | SGD 0.014 | — (cloud) |
| **Equivalent API cost** | SGD 1.94 | ~SGD 1.50 |
| **Quality** | 7/10 | 8/10 |
| **Notable issue** | 2 false-start synthesis turns; peak 236K input vs 98K loaded context (silent truncation) | Subagent doesn't produce trajectory.jsonl — only final summary metrics |

**Combined output: ~27 pages of research, 6,655 words, ~SGD 0.014 of GPU electricity.**

**Convergence between the two agents** (both surfaced these unprompted):
- Cross-session memory is THE #1 user-cited gap
- Proactive > reactive is the biggest value shift
- Privacy-first is meaningful for self-hosted, not just marketing
- Start narrow (memory + comm + calendar), expand outward

**Where they disagreed**:
- Voice on roadmap: Sentinel said "unrealistic on phone" → V5. Claude said "phone-as-thin-client + home-server-as-brain hits 2-3s round-trip via Whisper Small + Qwen3.6 + Piper TTS, equivalent to Home Assistant Voice PE" → V3.
- Cost framing: Sentinel implied self-hosting saves money. Claude argued SG-specific PUE + electricity tariff math makes it cost-NEUTRAL vs ChatGPT Plus (~SGD 30-40 vs SGD 27); the case is privacy + ownership, not cost.

---

## How to read the database

`benchmarks.yaml` schema (top-level fields per entry):

| Field | Source | Purpose |
|---|---|---|
| `id` | manual | Stable identifier (`<date>-<task-slug>-<agent>`) |
| `task`, `description` | manual | What was asked |
| `agent`, `model`, `quantization` | manual | Which configuration ran it |
| `parent`, `sibling_run` | manual | Link related benchmarks (e.g. baseline + expansion) |
| `trajectory`, `turn_range` | auto | For Sentinel runs — pointer back to source data |
| `wall_clock_seconds/minutes`, `model_invocations` | auto | Real-time consumption |
| `tokens.{input,output,total,peak_input_per_turn,ratio_in_to_out}` | auto | Compute consumption |
| `tool_calls.<name>` | auto | Activity profile |
| `compaction_count` | auto | Whether agent compacted history (see V6 prep doc on context handling) |
| `output_artifact.{file,words,chars,lines,pages_*}` | auto | Concrete deliverable measure |
| `cost_estimate.{electricity_sgd,api_equivalent_sgd,savings_sgd}` | auto | SGD-scale economics |
| `observed_issues` | manual | What went wrong / what's worth flagging |
| `quality_subjective_1to10` | manual | Your gut score (be honest) |
| `notes` | manual | Anything else |

---

## What to look for over time

As you accumulate entries, the patterns to watch:

1. **Wall-clock vs token-input ratio** — improving means LM Studio settings are getting better (prompt cache hits, batch size tuning). Backsliding means context is bloating without compaction.
2. **`peak_input_per_turn` vs `context_loaded`** — if peak ≫ loaded, you're silently truncating. Time to either raise context or add explicit OpenClaw compaction.
3. **`ratio_in_to_out`** — research tasks settle around 75-100:1; coding tasks might be 5-20:1; chit-chat 1-2:1. Way outside the typical band → bug.
4. **Per-page energy cost** — for a 9-page research output: SGD 0.0015 / page on local. Useful unit when planning capacity ("can I afford 100 research missions / week?" — yes, that's SGD 1.50).
5. **Quality score trend** — if you're gradually scoring runs lower, the agent persona / prompt has drifted. Re-tune.

---

## When to refresh the aggregate

The `aggregate` block at the bottom of `benchmarks.yaml` needs manual recompute when entries are added. Do it after every batch of 3-5 entries, not every single one.
