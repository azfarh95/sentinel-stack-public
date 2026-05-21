# Sentinel V3 — Handheld AI Wishlist (Expanded)

> Layered on top of `~/.openclaw/workspace/research/V3-handheld-AI-wishlist.md` (Sentinel agent's baseline).
> Date: 2026-05-09
> Method: Read Sentinel's output + 8 targeted searches across HN / r/LocalLLaMA / r/selfhosted / OpenAI community / vendor pricing pages + 3 primary-source deep reads.
> Author context: Singapore-based, GPU-equipped (24GB VRAM), Qwen3.6-27B in LM Studio, OpenClaw + MetaMCP + Telegram + Cloudflare Tunnel already shipped. V3 browser panel and mini-app already live.

---

## What Sentinel Got Right

Before adding, here is what holds up under deeper scrutiny. Each is a load-bearing claim that the second-pass evidence **strengthens** rather than weakens.

- **Memory is THE #1 frustration.** The OpenAI Developer Community has 300+ threads tagged with memory regression complaints, including two confirmed mass memory-wipe events (5 Feb 2025 and 6-7 Nov 2025) where users lost months of curated context overnight. Sentinel called this "scrambling to retrofit recall" — that wording is correct.
- **"Probabilistic retrieval feels random"** is a real and specific user complaint, not a vague gripe. Multiple threads quote users saying the assistant remembers a casual aside but forgets a directly stated preference. Sentinel's framing is accurate.
- **Proactive > Reactive is the value shift.** Confirmed by Home Assistant's Voice chapter 10 roadmap (June 2025) and Open WebUI's roadmap, both of which explicitly call out conversational follow-up + multi-turn proactivity as primary 2026 work.
- **Privacy/data-sovereignty resonates with self-hosters.** Reddit r/LocalLLaMA at 686k members, with the dominant building pattern being Ollama + Open WebUI + memory layer (Mem0 / Letta) — not a niche. The "your data stays on your device" pitch is genuinely the wedge.
- **"Start narrow, expand smartly."** Sentinel's prioritisation (memory + comms first, finance + health later) matches what self-hosted builders actually ship — most public personal-assistant projects on HN start with chat + memory, then layer one capability per release.

---

## What Sentinel Under-Explored

A surface-level pass on important topics. Each item below is a structural gap, not just a missing fact.

| Section | Sentinel's framing | What's missing |
|---------|-------------------|----------------|
| Memory | "ChatGPT memory is unreliable" — abstract | The specific failure modes, the two 2025 wipe events, the difference between OpenAI's flat-list memory vs. Mem0/Letta tiered memory. Sentinel cites Anthropic's "Search past chats" and OpenAI's PersonalContextAgentTool but doesn't name the open-source alternatives a self-hoster would actually deploy. |
| Proactive intelligence | "Users want it" | No engagement with the well-documented backlash: people **disable** Siri Suggestions and Google Now en masse. The 2026 wishlist isn't "more proactivity" — it's "proactivity I can trust and tune." Sentinel's "user-tunable sensitivity" line gestures at this but does not unpack it. |
| Cross-app automation | Generic "agent actions" | No mention of MCP (Model Context Protocol) as the 2025-26 wedge, despite the user already running MetaMCP. This is a glaring miss for this user. |
| Voice | "Real-time voice latency unrealistic" | True but stale. Home Assistant Voice PE + local Whisper fast-conformer + Piper TTS now achieves usable conversational latency on CPU/iGPU. Sentinel's reality check is from 2024 thinking. |
| Finance | "Bank API integration" | Singapore-specific gap: SGFinDex exists and is exactly the read-only personal-finance API Sentinel hand-waves about. Sentinel never mentions it. |
| Health | "Wearable API integration" | Apple Health export is one-shot, Google Fit is being deprecated for Health Connect. The actual self-hosted path is Gadgetbridge / Home Assistant integrations + manual MQTT. Sentinel's framing assumes US-style HealthKit → Cloud → AI flow that does not exist for self-hosters. |
| Privacy | "Local-first, encrypted store" | No mention of the harder problem: **multi-device sync** without breaking the local-first promise. Syncthing? Tailscale + central server? E2EE with cloud relay? This is the actual unsolved problem in the self-hosted space. |
| Sources | Vellum, DEV.to, Zapier, UX Collective, ZipTie, Budventure, Medium, Gartner, Plurality | All English-language, mostly US business-tech blogs. **Zero primary sources.** No Reddit, no HN, no GitHub README, no vendor pricing page, no actual user complaint thread. The synthesis is an SEO-blog synthesis, not a user-research synthesis. |

The sources gap is the most consequential. Sentinel is reading what marketers wrote *about* personal AI; this expansion reads what users wrote *to* personal AI vendors when it broke.

---

## New Findings (Beyond Sentinel's Baseline)

### A. The Self-Hosted Personal-AI Stack As It Exists Today

The 2026 reference stack — what people on r/selfhosted, r/LocalLLaMA, and HN are actually running — has converged on a recognisable pattern. The user already has most of the pieces.

**Inference runtime layer**
- Ollama (most popular, simplest)
- LM Studio (the user's choice — strong on macOS/Windows, weaker on headless Linux)
- vLLM (preferred for performance/throughput, less common for personal use)
- llama.cpp directly (power users)

**Memory layer (the new tier)**
- **Mem0** — 48k+ GitHub stars, framework-agnostic, three-tier (user/session/agent), hybrid store (vector + graph + KV). Free self-hosted, paid cloud.
- **Letta (formerly MemGPT)** — operates as a server with Python SDK, three-tier (core/archival/recall), supports local models via vLLM or Ollama. Tiered memory with retrieval depth, free self-hosted.
- **Zep** — production-focused, knowledge graph slant.
- **Supermemory / SuperLocalMemory** — newer entrants, less battle-tested.
- **ChromaDB** — used as raw vector store underneath custom memory implementations.

**UI / chat layer**
- Open WebUI (Ollama-native, RAG built in, STT via faster-whisper, TTS via coqui or OpenAI)
- AnythingLLM (workspace-first, hybrid search + reranking, GitHub/Confluence/Drive connectors)
- LibreChat (closer to ChatGPT clone)
- **Telegram bots backed by n8n workflows** — this is a real and growing pattern; n8n.io has a published "privacy-focused AI assistant with Telegram + Ollama + Whisper" template.

**Automation / orchestration**
- n8n (most common visual orchestrator for personal AI)
- Home Assistant (anchor for voice + IoT + LLM conversation agent)
- OpenClaw + MetaMCP (the user's stack — niche but coherent; not yet showing up in public stack surveys)

**Implication for Sentinel V3:** the user is not building in a vacuum. The dominant pattern out there is **"n8n + Ollama + Telegram, glued together by hand."** Sentinel V3's differentiator is *not* "we have the pieces" — it is **"the pieces are pre-integrated, the memory is structured, and there is one identity (the user) across all of them."** That coherence is exactly what the n8n-glue stack lacks.

### B. The Memory Problem — Specific Failure Modes

Sentinel said "ChatGPT memory is unreliable." The actual user complaints break down into five distinct modes, each with a different fix:

1. **Catastrophic wipes from backend updates.**
   - Feb 5 2025: a backend deploy at OpenAI wiped saved memories for thousands of users; r/ChatGPTPro had 300+ threads.
   - Nov 6-7 2025: a second wipe hit again.
   - **Fix the user can't get from cloud:** memory you own, in a database you can back up.
2. **Silent non-saves.** "It says it remembers, the memory panel is empty." Caused by the autosave heuristic deciding the fact wasn't important enough.
   - **Fix:** an explicit `memory_store` tool the user invokes (the user already has this via mcp-memory-service in Claude Code).
3. **Memory full / quota silently capping.** Free-tier ChatGPT memory hits a ceiling and starts evicting; users only notice when older facts vanish.
   - **Fix:** unbounded local store, with eviction policy under user control.
4. **Cross-thread context loss.** GPT-4o specifically had a regression where memory was saved but not retrieved into new threads (logged in OpenAI Developer Community thread `bug-gpt-4o-memory-regression-1310926`).
   - **Fix:** memory injected at the orchestrator level (MetaMCP), not at the model level.
5. **Recall feels random.** The complaint "it remembers I like coffee but forgot I'm a vegetarian" — flat-list semantic retrieval prioritises recency and lexical similarity over importance.
   - **Fix:** tiered memory (Mem0/Letta-style core+archival), where high-value facts are pinned in-context.

**The Sentinel V3 lesson:** "memory" is not one feature, it is at minimum five failure modes that users have catalogued in painful detail. Building "memory" without naming which of these you fix is the trap.

### C. Proactive AI — The Trust Calibration Problem

Sentinel framed proactivity as a clear win. The deeper truth: proactive AI is the feature most likely to be **disabled** within a week of install. The user-research literature names this "alert fatigue" and the iOS settings ecosystem testifies to it.

**Apple's design admits the problem.** The path to disable proactive features in iOS is exhaustive: Settings → Siri & Search has per-app toggles, plus separate switches for Lock Screen Suggestions, Search Suggestions, Look Up Suggestions, location-based "Suggestions & Search" buried under Privacy → Location Services → System Services. Apple built this many escape hatches because users demanded them.

**Why proactive notifications get killed:**
1. **No actionability.** "Traffic is bad" without a "reschedule the meeting?" button is just stress.
2. **Wrong cadence.** A nudge twice a day is a feature; ten times a day is uninstall-ware.
3. **Wrong precision.** "You haven't exercised in 3 days" delivered while you're sick is an insult.
4. **No mute affordance.** The user wants to say "not this week" without burning the whole feature.

**What works (from the literature):**
- Single-tap "snooze for X" on every proactive notification.
- Per-category sensitivity (calendar nudges = on, exercise nudges = silent for now).
- "Why did you tell me this?" trace — show the rule/event that triggered it. This is a key Sentinel-specific opportunity: the user already has agent transcripts; surface them.
- Confidence gating — only fire a proactive notification if the underlying signal is above a confidence threshold the user can tune.

**Sentinel V3 implication:** proactive features should ship behind an explicit opt-in and with a per-category mute. The "always-on" framing is the trap. Proactivity is a bicycle, not a motorcycle — the user pedals (sets up the rules), the system amplifies.

### D. Self-Hosted-Specific Concerns Sentinel Missed

Sentinel's framing assumes the user is building for a generic "self-hoster." The user is in Singapore, in a tropical apartment, on the same machine that hosts the Crib Watchdog inference bridge. The constraints are sharper.

**D.1. Tropical heat + 24/7 GPU is real.**
- The XDA piece on running local LLMs in expensive-energy markets (Ireland, $0.62/kWh peak) reports the system idles at ~70W and inference spikes between 150-250W. The author says the bill barely moved.
- Singapore residential tariff (Q2 2026) is 27.27 ¢/kWh ex-GST, ~29.72 ¢/kWh inc-GST. That is roughly half of Ireland's peak rate.
- However: **PUE for tropical climates is 1.5-1.8x.** Every watt the GPU produces is a watt the aircon must remove. A 70W idle becomes ~115W effective load. A 200W inference spike becomes ~330W effective.
- Math: 70W idle × 24h × 30d × 1.6 PUE × $0.30/kWh ≈ **SGD 24/month** for idle alone. Add inference and it's plausibly SGD 30-40/month for a moderate-use personal assistant.
- ChatGPT Plus is USD 20/month ≈ SGD 27/month. Claude Pro is USD 20/month ≈ SGD 27/month.
- **Conclusion: the local-LLM-vs-subscription cost-savings argument is roughly break-even in Singapore at moderate use.** The win is privacy + customisation + memory ownership, NOT cost.
- This must be communicated honestly to the user / future README. "Local is cheaper" is a US argument; in Singapore it isn't.

**D.2. What happens when the user travels.**
- Sentinel never addresses this. Cloudflare Tunnel solves the connectivity problem (the user already has it) but not the resilience problem.
- If the home server crashes while the user is in Tokyo, the assistant goes dark. Cloud-based ChatGPT does not have this failure mode.
- Mitigation: a small fallback tier — when the home server is unreachable, fall back to OpenRouter or a cloud Claude/Gemini call, while **flagging in the response** that fallback is active. This is non-trivial design but matches Sentinel's existing watchdog culture.

**D.3. Family / multi-user considerations.**
- The user lives with someone (the "wife" in earlier project context). The assistant's memory is currently single-tenant.
- If the partner ever asks the assistant a question, three problems: (a) wrong identity, (b) memory pollution, (c) privacy leak (the partner sees the user's prior context).
- Telegram group context complicates this further — Sentinel V2's `visibleReplies` and `ownerAllowFrom` keys are doing the right thing here but only partially.
- Sentinel V3 should treat **identity-aware memory** as a category, not as an afterthought. Even a single-user system needs at least a "guest" mode.

**D.4. Telegram-first comms culture.**
- Singapore is a Telegram-heavy market for tech-adjacent users. WhatsApp is dominant for general comms but Telegram is where async dev/work happens.
- The user has shipped V3 mini-app already, which means inline keyboards + web app launch + media reply are all available. This is a UI surface that no US-centric "personal AI" product is designed for.
- Sentinel's "Communication Hub" section assumes email + WhatsApp + SMS. For this user, **Telegram IS the hub.** Email/SMS are tributaries.

**D.5. Multi-device sync without breaking local-first.**
- Sentinel implies "local-first = good, cloud = bad." Reality is more nuanced.
- If the assistant lives only on the home server, the phone is a dumb terminal. That works (Telegram-as-UI is exactly this) but means there is no offline mode on the phone.
- Real-world self-hosters use Tailscale + a dedicated subnet for remote access, or Syncthing for selective state replication. Neither solves the "phone offline" problem.
- The user has Cloudflare Tunnel which is fine for inbound access but introduces an external dependency. If Cloudflare has an outage, the bot goes dark.
- **Honest framing:** the project IS centralised on the home server. Don't pretend otherwise. The local-first claim is "local to your home, not local to your phone." Different but still meaningful.

### E. Willingness-to-Pay Signals

Sentinel did not engage with monetisation at all. For a self-hosted project this looks irrelevant — but pricing data is useful as a **proxy for what features people consider valuable enough to pay for.** That informs scope.

**Memory-first AI pricing (2026):**
- Mem.ai (Mem 2.0): USD 12/month Pro (launched 1 Oct 2025). Free tier: 25 notes + 25 chats per month — very limited.
- Mem0 (the dev framework, not the consumer app): free self-hosted, paid cloud at usage tiers.
- Rewind.ai: was USD 19/month annual / USD 29 monthly. **Acquired by Meta in Dec 2025, rebranded to Limitless, original Rewind app sunset.** Notable: a market-validated personal-memory product was acquired by Big Tech rather than scaling independently — a signal that monetising memory-first AI as a standalone consumer product is hard.
- ChatGPT Plus: USD 20/month.
- Claude Pro: USD 20/month.
- Claude Max: USD 100-200/month (where serious users actually go).

**Consumer survey data (mixed):**
- Statista 2025: most US consumers are NOT willing to pay for an AI personal assistant.
- Suzy 2025: 37% of users WOULD pay for generative AI tools.
- a16z's "State of Consumer AI 2025": "Products that embed persistent memory, have strong integrations, and unlock and train with proprietary user data are seen as having strong defensibility."

**Implication for Sentinel V3:**
- The user is not selling Sentinel, but the pricing data tells us where the market sees value: persistent memory + integrations + proprietary-data fine-tuning are the moats.
- If the user ever opens Sentinel as a product to others, pricing should be in the USD 15-25/month range (the established personal-AI band) NOT in the productivity-suite range.
- Rewind's acquisition by Meta is a red flag for any "memory product" lock-in narrative — the data is so valuable that Big Tech buys it. Self-hosted is the actual escape hatch from this dynamic.

### F. Specific Communities / Voices Worth Watching

Things Sentinel cited zero of, but where the actual signal lives:

**Subreddits**
- r/LocalLLaMA (686k members) — model releases, hardware, jailbreaks; weight on inference quality.
- r/selfhosted — broader self-hosted infra; assistant-related threads tagged "AI" / "LLM."
- r/HomeAssistant — voice + LLM + IoT integration; check the "Assist" tag.
- r/OpenWebUI — concrete user issues with the dominant self-hosted UI.
- r/ChatGPTPro — treat as a complaints firehose; useful for failure-mode discovery.

**HN searches worth saving**
- `personal AI assistant memory` — yields the goto-assistant, Mai, and Leon discussions.
- `Show HN local LLM` — quarterly stack snapshots.
- `Ask HN self-hosted` — long-form thread responses with concrete configs.

**Specific projects worth tracking**
- **Letta** (letta.com / GitHub) — academic-rigorous memory; open core.
- **Mem0** (mem0.ai / GitHub) — pragmatic memory; framework-agnostic.
- **Home Assistant Voice PE** — the only mainstream local-voice project with proper roadmap discipline.
- **Open WebUI** (openwebui.com) — the chat layer most self-hosters end up on; useful as a comparison surface.
- **n8n** (n8n.io) — the orchestration layer; their workflow templates are a feature backlog written by users.
- **AnythingLLM** — workspace model is interesting prior art for multi-context memory.

**Blog series**
- The XDA Developers self-hosted-LLM coverage is unusually grounded (real numbers, real complaints).
- Home Assistant blog "Voice chapter X" series — quarterly updates on local-voice progress.
- a16z "State of Consumer AI" series — annual; commercial slant but useful market signal.
- r/LocalLLaMA's weekly "What did you build this week?" threads — best low-noise signal of what people actually ship.

---

## Sentinel and Claude — Where We Agreed and Disagreed

| Topic | Sentinel claim | Claude (this expansion) | Verdict |
|-------|----------------|------------------------|---------|
| Memory is #1 frustration | Yes, biggest single gap | Yes, with five distinct failure modes | **Agree, expanded** |
| Proactive intelligence is the value shift | Yes, design for it | Yes, BUT it is also the most-disabled feature; mute-by-default is required | **Disagree on framing** |
| Cross-app automation via agent actions | Generic claim, no protocol cited | MCP is the obvious 2026 substrate; the user already runs MetaMCP | **Sentinel missed the obvious** |
| Voice latency on local infra is unrealistic | Yes, hybrid model | Stale — HA Voice PE shows usable local-voice exists in 2026 | **Disagree, Sentinel is one cycle behind** |
| Privacy is a top-3 selling point | Yes | Yes, but the multi-device-sync problem means "local-first" is a story, not a property | **Agree with caveat** |
| Health/wearable integration | Easy with APIs | Health Connect / HealthKit lock-in makes self-hosted health hard; Gadgetbridge is the realistic path | **Sentinel under-specified** |
| Finance integration via banking APIs | Generic | Singapore-specific: SGFinDex exists and is read-only-personal-finance by design | **Sentinel missed regional context** |
| "Always-on ambient listening" is unrealistic | Yes | Yes, BUT push-to-talk via Telegram voice notes is a viable workaround the user already has | **Agree, Sentinel under-credited the existing UI** |
| Local LLM is cheaper than subscriptions | Implied | Roughly break-even in Singapore due to PUE 1.5-1.8x; the win is privacy not cost | **Disagree** |
| Top 10 ranking | Memory > Proactive > Agent actions > Context > Comms > Calendar > Finance > Privacy > Knowledge > Health | Same top 4, but I'd swap **Comms ↑** and **Finance ↓** for a Singapore Telegram-first user. Knowledge graph is also higher than Sentinel ranked it given the user's research workflow. | **Mostly agree, regional re-weighting** |

---

## One Specific Disagreement, Unpacked

**Sentinel:** "Real-time voice conversations require cloud APIs because local LLM inference on a phone cannot match cloud-hosted models for <500ms latency."

**Claude (me):** This is true if you're trying to run voice on the **phone**. It is no longer true if you accept that the phone is a thin client and the inference happens on the home server, which is the user's existing topology.

The 2026 reality:
- Whisper Small / faster-whisper on the home GPU does sub-second STT on a 24GB card.
- Piper TTS on CPU is real-time on commodity hardware.
- Qwen3.6-27B at int4 on a 24GB GPU does ~30-50 tok/s.
- A 50-token reply is generated in ~1s; round-trip including network is 2-3s.
- Home Assistant Voice PE explicitly demonstrates this works (their Voice chapter 10 release was June 2025).

So Sentinel's reality-check is **correct for on-device, wrong for the user's actual home-server topology.** This matters because it means voice is on the table for V3, not deferred to V5. The constraint is bandwidth (mobile uplink → tunnel → server) and Cloudflare Tunnel's WebSocket behaviour, not raw compute.

That said, **typing in Telegram is fine and arguably better** for most use cases (silent, quotable, async). The disagreement is about whether voice should be on the roadmap, not about whether to ship it next.

---

## Recommended Sentinel V3 Scope (Synthesised)

Drawing from BOTH analyses, ordered by leverage-given-the-user's-existing-stack. Each item names the prior art so the user can compare against what already exists rather than reinventing.

### Tier 1 — Foundation (do first, leverage is highest)

1. **Tiered memory layer (episodic + semantic + procedural).**
   - Prior art: Mem0, Letta. The user already has mcp-memory-service installed in Claude Code at user scope (per memory note). Extend the same store to OpenClaw / MetaMCP so all assistants see one identity.
   - Failure modes to specifically prevent: silent non-save, cross-thread loss, recall-feels-random. Pin high-value facts ("vegetarian," "wife's name," "current projects") to a "core" tier that always loads.
   - Auditability is non-negotiable: a Telegram command `/memory list` and `/memory delete <id>` from day one.

2. **MCP-native action surface.**
   - The user already runs MetaMCP. Sentinel V3 should treat MCP tools as the action API, not invent a parallel one.
   - Specifically: every "agent action" Sentinel takes should be a tool call visible in MetaMCP's logs. This is the trust-calibration mechanism.

3. **Identity-aware context.**
   - Even single-user, ship a "this is Azfar" claim that propagates through every agent call. This sets up multi-user later without rewrites.
   - Bonus: `ownerAllowFrom` (already in V2) is the prior art; extend it to memory writes.

### Tier 2 — Proactive layer (do once foundation is solid)

4. **Calendar + comms proactive nudges, opt-in per category, mute-able from any notification.**
   - One-tap snooze: "/snooze 7d" or inline keyboard.
   - Trace-ability: every proactive nudge ends with "(triggered by: rule X / event Y)" — so the user can tune.
   - Start with calendar (highest signal, lowest false-positive rate) before exercise/finance.

5. **Cross-thread context unification.**
   - The user has multiple tool namespaces (per memory note: Sentinel architecture has multiple bots). Make sure memory writes from any bot are readable by any other bot for the same user.
   - This is a memory-router problem more than a memory-store problem.

### Tier 3 — Capability expansion (do once Tier 1+2 is stable)

6. **Communication triage** — Telegram-first, not email-first. The user's actual inbox is Telegram. Email triage is a later enhancement.
7. **Knowledge graph for research** — index the existing `~/.openclaw/workspace/research/` directory; let queries like "what did I research about V3 last month?" hit it. This document itself should be ingested.
8. **Finance** — SGFinDex integration if the user wants it; otherwise skip. Don't invent banking integration.
9. **Health** — defer until there's a clear use case the user actually wants. Health is the most over-promised category in personal-AI.
10. **Voice** — defer behind keyboard interaction. Add only when there's a concrete bottleneck the user actually hits.

### Tier 4 — Cross-cutting concerns (always on)

- **Travel resilience.** Cloud fallback when home server is unreachable, with explicit "FALLBACK MODE" labelling in responses. This is a small feature with large psychological payoff.
- **Cost transparency.** A `/cost` command that shows GPU power draw + estimated SGD this month. Builds trust and informs scope.
- **Honest local-first framing.** README should say "local to your home, not local to your phone." Don't oversell.

---

## Sources Added (beyond Sentinel's 9)

1. [Hacker News — Ask HN: What Does Your Self-Hosted LLM Stack Look Like in 2025?](https://news.ycombinator.com/item?id=44187275) — primary source for hardware/stack patterns (RTX 3090/4090, M3 Max, Ollama+Open WebUI dominance).
2. [OpenAI Developer Community — ChatGPT memory broken at the moment](https://community.openai.com/t/chatgpt-memory-broken-at-the-moment/1108272) — primary failure-mode evidence.
3. [OpenAI Developer Community — GPT-4o memory regression: context loss across chats](https://community.openai.com/t/bug-gpt-4o-memory-regression-context-loss-across-chats-and-inside-threads/1310926) — specific cross-thread memory bug.
4. [TechRadar — ChatGPT memories are disappearing for some users](https://www.techradar.com/ai-platforms-assistants/chatgpt/chatgpt-memories-are-disappearing-for-some-users-heres-what-you-can-do-to-protect-yours) — coverage of the Feb 2025 wipe.
5. [Mem0 — pricing & docs](https://mem0.ai/pricing) — memory-framework pricing and architecture.
6. [Mem.ai — pricing](https://get.mem.ai/pricing) — consumer memory-app pricing.
7. [Vectorize — Mem0 vs Letta (MemGPT) compared](https://vectorize.io/articles/mem0-vs-letta) — head-to-head of the two open-source memory frameworks.
8. [DEV.to — 5 AI Agent Memory Systems Compared (Mem0, Zep, Letta, Supermemory, SuperLocalMemory)](https://dev.to/varun_pratapbhardwaj_b13/5-ai-agent-memory-systems-compared-mem0-zep-letta-supermemory-superlocalmemory-2026-benchmark-59p3) — benchmark numbers.
9. [Home Assistant — Voice chapter 10 release notes](https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/) — local-voice-LLM roadmap.
10. [Home Assistant — Voice Preview Edition product page](https://www.home-assistant.io/voice-pe/) — current state of local voice hardware.
11. [Home Assistant — Building the AI-powered local smart home](https://www.home-assistant.io/blog/2025/09/11/ai-in-home-assistant/) — assist conversation agent direction.
12. [n8n — Privacy-focused AI assistant with Telegram, Ollama, Whisper](https://n8n.io/workflows/6012-create-a-privacy-focused-ai-assistant-with-telegram-ollama-and-whisper/) — reference workflow this user can compare against.
13. [Liz-in-Tech blog — Building a Telegram Personal Assistant with n8n](https://liz-in-tech.github.io/blog/posts/llm/039_n8n.html) — concrete writeup of the dominant pattern.
14. [Medium — I migrated my own AI assistant to Telegram (Open WebUI + Qwen 14B)](https://medium.com/becoming-for-better/i-migrated-my-own-ai-assistant-to-telegram-local-llm-open-webui-bot-4b63ab757217) — direct analogue to the user's stack.
15. [XDA Developers — I run local LLMs in one of the world's priciest energy markets, and I can barely tell](https://www.xda-developers.com/run-local-llms-one-worlds-priciest-energy-markets/) — primary numbers on idle/inference power.
16. [Spheron — AI Inference Power Consumption and GPU Electricity Costs 2026](https://www.spheron.network/blog/ai-inference-power-electricity-cost-2026/) — PUE math for tropical climates.
17. [SP Group — Singapore electricity tariff revision for Q2 2026](https://www.spgroup.com.sg/our-services/utilities/tariff-information) — local electricity baseline.
18. [Apple Support — Turn Siri Suggestions on or off](https://support.apple.com/guide/iphone/turn-siri-suggestions-on-or-off-iph6f94af287/ios) — primary evidence that proactive features need granular off-switches.
19. [Apple Privacy — Siri Suggestions and Search](https://www.apple.com/legal/privacy/data/en/siri-suggestions-search/) — Apple's own description of the trust calibration problem.
20. [Rewind / Limitless pricing](https://www.rewind.ai/pricing) — memory-first consumer AI pricing baseline.
21. [a16z — State of Consumer AI 2025](https://a16z.com/state-of-consumer-ai-2025-product-hits-misses-and-whats-next/) — market signal on memory-product defensibility.
22. [Atlan — Best AI Agent Memory Frameworks in 2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/) — comparative landscape.
23. [MachineLearningMastery — The 6 Best AI Agent Memory Frameworks You Should Try in 2026](https://machinelearningmastery.com/the-6-best-ai-agent-memory-frameworks-you-should-try-in-2026/) — ranked overview.
24. [GitHub — agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw) — Qwen-ecosystem personal assistant; comparable target.
25. [GitHub — Sh1nr1/mai-ai-assistant-self-hosted](https://github.com/Sh1nr1/mai-ai-assistant-self-hosted/) — multi-tier memory implementation reference.

---

## Limitations of This Expansion

Honest about what is thin:

- I could not deeply verify the **Feb 2025 / Nov 2025 ChatGPT wipe events** from primary OpenAI sources — only via TechRadar coverage and forum aggregations. Plausible and consistent across sources but treat the specific dates as approximate.
- The Singapore tariff math uses a generic PUE multiplier; the user's actual PUE depends on their aircon setup. The number (SGD 24-40/month) is order-of-magnitude correct but not a personal estimate.
- I did not run a primary search of r/LocalLLaMA threads — only via aggregators (`aitooldiscovery.com/guides/local-llm-reddit`). Direct reddit.com results were sparse in the search engine results pages, possibly due to Reddit's anti-scraping posture in 2026.
- "Willingness to pay" is a soft signal at best for a self-hosted project that is not for sale. I included it as feature-importance proxy.
- I did not search SGFinDex specifically; mentioned because I know it exists from Singapore fintech context, not because a search verified its current API surface.
- Home Assistant Voice PE latency claims are from their own marketing. Real-world performance depends heavily on hardware and network.

---

*Document compiled by Claude Opus 4.7 as a second-pass research expansion on top of Sentinel agent (Qwen3.6-27B local) baseline. 2026-05-09.*
