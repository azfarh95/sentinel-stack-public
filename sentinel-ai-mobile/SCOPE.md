# Sentinel AI Mobile — Scope

> The phone-side AI node of the Sentinel mesh. Turns the owner's phone (HONOR
> Magic7 Pro · Snapdragon 8 Elite · 12 GB) into a first-class **tailnet compute +
> sensor node** running on-device models. "Phone = fast lane" productised.

Status: **scoping** · Owner-only · Tailnet-secured
Seed module already shipped: `sentinel-translate-keyboard` (v0.5.0)

---

## 1. Why now — the proof
Gemma-4-E4B (LiteRT, ~3.7 GB) validated **on-device, on the Adreno GPU**:
- read **Cyrillic off a screenshot** (no OCR/Tesseract step),
- produced a **context-aware** translation — decoded slang (`Щас`→"now"), flagged
  idiom (`Учу билеты` = exam topics, not literal "tickets"), added cultural notes,
- ~30.5 s, RAM held within the 12 GB budget.

That out-reasoned the ML Kit / LibreTranslate pipeline on nuance — the exact quality
jump the project is built on. **Thesis proven; this scope productises it.**

---

## 2. Principles (the guardrails that keep this from becoming a Dove-style saga)
1. **Node, not brain.** Never replaces Qwen-27B. It's a tier, not a takeover.
2. **Graceful availability = optimisation, not dependency.** Tools deregister when
   the phone sleeps / is away / on cellular; Dove falls back to Qwen. The phone
   being offline must never break a Dove turn.
3. **Owner-only + tailnet** is the security boundary (same as the rest of the suite).
4. **On-device by default for sensitive data.** Screen / photos / mic / location
   stay on the phone; nothing ships to a server unless the user picks a server engine.
5. **Load-on-demand.** The model spins up per request, not a 24/7 daemon — battery/thermal.

---

## 3. Architecture — where it sits in the mesh

```
            ┌─────────────────────────── SENTINEL MESH (tailnet) ───────────────────────────┐
            │                                                                                │
   ┌────────▼─────────┐                                                   ┌──────────────────▼────────────────┐
   │  BOX (smart lane)│                                                   │   PHONE (fast lane)               │
   │  sentinel-host   │                                                   │   Sentinel AI Mobile              │
   │                  │                                                   │                                   │
   │  Dove / OpenClaw │── metamcp tool call ──►  phone.* MCP tool  ──tailnet──►  on-device tool server         │
   │  Qwen-27B (brain)│◄── result / "unavailable→fallback to Qwen" ◄──────────  Gemma-4-E4B (+ Qwen3-VL-8B)    │
   │  always-on       │                                                   │  sees: screen, camera, mic, GPS   │
   │  server data     │                                                   │                                   │
   └──────────────────┘                                                   │  app calls UP → suite services    │
            ▲                                                             └───────────────┬───────────────────┘
            └──────────────────────── suite services (Caddy edge) ◄───────────────────────┘
```

- **Down:** Dove → metamcp → `phone.*` tools (the phone executes one bounded job).
- **Up:** the app calls suite services (Home, Finance, Watchdog…) like the other apps.
- **Transport:** tailnet (Headscale) + Caddy `*.svc.your-domain.example.com` edge for HTTPS.

---

## 4. Components / modules

| # | Module | Responsibility |
|---|--------|----------------|
| A | **Inference core** | Host on-device models. LiteRT-LM (Gemma-4-E4B, default) + MNN/llama.cpp (Qwen3-VL-8B, upgrade). Load-on-demand, GPU/NPU. |
| B | **Tool server** | Expose fast-lane capabilities over tailnet (OpenAI-compatible HTTP or native MCP). Foreground service. |
| C | **Assistant UI** | Chat · vision (camera + screenshot/share-sheet) · voice. "Dove Lite" for local/offline queries. |
| D | **Keyboard module** | The existing Sentinel Translate Keyboard (type-translate + on-screen bubble). Folds in / shares the model. |
| E | **Suite integration** | Apps-hub publish, Watchdog registration (best-effort node), tailnet identity. |
| F | **Availability/heartbeat** | Register tools when awake+charging+wifi; deregister on Doze. Drives the graceful fallback. |

---

## 5. The fast-lane toolset (MCP tools Dove can call)
From the reliability map — bounded, single-shot, **input lives on the phone**:

| Tool | E4B reliability | Notes |
|------|-----------------|-------|
| `phone.translate` | ✅ | text or image; context-aware |
| `phone.ocr` | ✅ | read text from image, any script |
| `phone.vision` | ✅ | caption / "what's in this" / VQA |
| `phone.summarize` | ✅ | notification / message / short doc |
| `phone.classify` | ✅ | urgent? sentiment? language-ID |
| `phone.extract` | ✅ *verify* | single clear field (OTP, date, amount) |
| `phone.transcribe` | ✅ | audio → text (Gemma E-series audio) |
| `phone.draft` | ✅ | rephrase / draft reply |

**Reliability levers (apply to every tool):** one-shot, constrained output
(schema/grammar), terse prompts, **verify programmatically** (never trust digits).
Qwen does the tool-*selection*; the phone model is the *engine behind one tool*.

**MCP tool contract:** `phone.*` registers in metamcp → backend forwards to the
phone tool-server over tailnet → **graceful-fallback wrapper** (phone unreachable →
return `unavailable` → Dove falls back to Qwen).

---

## 6. Tech stack (recommendations — see §9 for the open calls)
- **App:** native **Kotlin** (recommended — the ML runtimes LiteRT/MediaPipe/MNN are
  native, the keyboard is already Kotlin; Flutter would need platform channels).
  *Home stays Flutter; this one is native for tight model integration.*
- **Model runtime:** **LiteRT-LM** (Gemma-4-E4B) primary; **MNN / llama.cpp+mmproj**
  (Qwen3-VL-8B) as the upgrade tier. NPU (Hexagon/QNN) later for speed/battery.
- **Tool transport:** phone runs a small local server reached over tailnet; metamcp
  wraps it as `phone.*`. (HTTP-OpenAI vs native-MCP = open call, §9.)
- **Models:** Gemma-4-E4B (default), Qwen3-VL-8B (upgrade), audio via Gemma or Whisper.
  **Download-on-demand**, not bundled (3.7 GB+).

---

## 7. Security model
- Tool endpoint is **tailnet-only** (owner mesh) + a shared token; never public.
- **No secrets in the APK.**
- Screen/accessibility/photo data is processed **on-device**; only leaves if a server
  engine is explicitly chosen.
- App ↔ suite uses the existing Caddy edge + owner auth.

---

## 8. Phasing / roadmap
- **Phase 0 ✅** — validate E4B on-device (done: 30.5 s, context-aware, RAM held).
- **Phase 1 ✅** — keyboard + on-screen bubble (shipped, `sentinel-translate-keyboard` v0.5.0).
- **Phase 2** — *the proof slice*: phone tool-server with **1–2 tools** (`phone.vision`/
  `phone.translate`) + metamcp entry + graceful-fallback. **Dove calls your pocket.**
- **Phase 3** — the app shell: chat/vision/voice UI + tool server + apps-hub publish +
  Watchdog registration; absorb the keyboard as a module.
- **Phase 4** — expand toolset (summarize/extract/transcribe), Qwen3-VL-8B upgrade tier,
  NPU acceleration, latency work (terse prompts, caching).

---

## 9. Decisions — LOCKED 2026-06-19
1. **App framework** → **native Kotlin** (ML runtimes are native; keyboard already Kotlin).
2. **Tool transport** → **OpenAI-compatible HTTP on the phone + a thin box-side MCP shim**
   that forwards with the graceful-fallback wrapper.
3. **Keyboard** → **keep separate; share ONE on-device model via a shared inference
   service** (only one 3.7 GB model resident; keyboard keeps working as-is today).
4. **Phase-2 first tool** → **`phone.vision`** (image + prompt → answer; translate is a subset).

## 11. Phase 2 — the proof slice (`phone.vision`)
**Goal:** Dove sends an image + prompt → gets the model's answer *from the phone*;
falls back to Qwen-VL on the box if the phone is offline.

**Phone — `Sentinel AI Mobile` (minimal Kotlin app):**
- Foreground service `InferenceService`:
  - loads **Gemma-4-E4B** (LiteRT-LM, GPU) on first request, keeps warm for a TTL.
  - embeds a tiny HTTP server (Ktor / NanoHTTPD) on `0.0.0.0:<port>`, tailnet-reachable.
  - `POST /v1/chat/completions` — OpenAI-compatible, accepts **image (base64) + text**.
  - `GET /health` — drives the heartbeat.
  - shared-secret token header.
- This same service is what the **keyboard** later calls for its premium VL mode (one model in RAM).
- Tailnet: box reaches the phone at its MagicDNS name / `100.x:<port>`; ACL allows box→phone.

**Box — `phone-vision-mcp` (FastMCP, mirrors `pdf-mcp`):**
- Tool `phone_vision(image, prompt) -> {text, source: "phone" | "fallback"}`.
- Forwards to the phone's `/v1/chat/completions` over tailnet.
- **Graceful fallback:** timeout / unreachable / health-fail → `available:false` → Dove
  routes to the box's Qwen-VL (mmproj) instead. Phone is an optimisation, never a dependency.
- Config (phone URL + token) from `.env.local` / Watchdog manifest. Registered in metamcp `default`.

**De-risk build order:**
1. ✅ **DONE 2026-06-19** — `phone-vision-mcp` built (`metamcp-local/phone-vision-mcp/`, FastMCP,
   mirrors pdf-mcp), running healthy, **registered ACTIVE in metamcp default namespace**
   (`register_phone_vision.sql`, server uuid `…008100`, url `http://phone-vision-mcp:8100/mcp`).
   Vision path **verified end-to-end** against the box Qwen-VL (`qwen/qwen3.6-27b`): transcribed +
   translated a Russian chat screenshot faithfully. `PHONE_VL_BASE` unset → Qwen-VL only for now.
   Host debug port `127.0.0.1:8102` (8100 was taken). `phone_vision(image, prompt, prefer)` →
   `{text, source, model}`; image = /data file | http(s) URL | base64/data-URL.
2. **Build the minimal Android `InferenceService`** + HTTP endpoint; set `PHONE_VL_BASE` →
   the shim then prefers the phone, falls back to Qwen-VL.
3. Add heartbeat/availability + token auth; verify graceful fallback by sleeping the phone.

---

## 10. Risks & mitigations
| Risk | Mitigation |
|------|------------|
| Android Doze kills the tool server | foreground service + **graceful degrade** (Dove falls back) |
| 30 s latency | terse prompts, NPU, response caching, keep verbose mode opt-in |
| Battery/thermal on sustained use | load-on-demand, not 24/7; throttle |
| Headscale/tailnet routing flakiness (just lived it) | tools must tolerate transient unreachability — that's the whole graceful-fallback design |
| 8B (Qwen3-VL) RAM pressure | keep E4B default; 8B opt-in; watch ZRAM |
