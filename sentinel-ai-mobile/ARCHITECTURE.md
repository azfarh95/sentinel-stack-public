# Sentinel AI Mobile — App Architecture

Native Kotlin / Android. Companion to `SCOPE.md` (mesh role, locked decisions,
phasing). This doc designs **the app itself**: layers, the inference core, the
tailnet tool-server, data, lifecycle, mesh integration, and the build milestones.

Reference points: AI Edge Gallery (LiteRT runner + chat — our inference baseline,
open-source) and Open WebUI (assistant UX — chat, history, model mgmt, skills).

---

## 0. Design goals & constraints
- **One resident model**, shared by app UI + keyboard + Dove. RAM is the budget
  (12 GB real, ~5–7 GB usable after MagicOS).
- **Local-first & private** — inference on-device; screen/photos/mic stay on the phone.
- **Mesh node** — expose `phone.*` over tailnet for Dove, with graceful availability.
- **Pluggable engines** — LiteRT (Gemma-4-E4B) now; MNN/llama.cpp (Qwen3-VL-8B)
  later, behind one interface, **no UI churn**.
- **Battery/thermal aware** — load-on-demand, keep-warm TTL, unload on idle.

## 1. Layered architecture

```
┌──────────────────────────────────────────────────────────────┐
│ UI  (Jetpack Compose · single-Activity · Navigation)         │
│  features: chat · vision · voice · models · skills · settings│
├──────────────────────────────────────────────────────────────┤
│ Presentation  (ViewModels · UI state · MVVM)                 │
├──────────────────────────────────────────────────────────────┤
│ Domain  (use cases · models: Conversation/Message/ModelSpec/ │
│          Tool · repository interfaces)                         │
├───────────┬───────────────────────┬──────────────┬───────────┤
│ Data      │ Inference core        │ Server       │ Mesh      │
│ Room +    │ InferenceEngine       │ Ktor embed   │ tailnet   │
│ DataStore │  ├ LiteRtEngine       │  /v1/chat…   │ identity  │
│ + files   │  └ (Llama/MNN later)  │  /health     │ + token   │
│           │ ModelManager          │  /v1/models  │ + heartbt │
│           │ InferenceService  ◄── foreground service owns the model
└───────────┴───────────────────────┴──────────────┴───────────┘
       ▲ in-proc          ▲ localhost:port          ▲ tailnet:port
    app UI             keyboard (other app)       Dove via metamcp shim
```

## 2. The inference core (the crown jewel)
- **`InferenceEngine`** (interface): `load(spec)` / `unload()` / `capabilities()` /
  `generate(req): Flow<Chunk>` (token streaming).
- **`LiteRtEngine`**: Gemma-4-E4B via **LiteRT-LM / MediaPipe LLM Inference**, GPU.
  Multimodal image now; audio later. (AI Edge Gallery is the open-source reference.)
- **`ModelManager`**: installed-model registry; **single-resident policy** (one model
  in RAM — evict before loading another); keep-warm TTL; download (HF LiteRT) + import
  (local `.litertlm`).
- **`InferenceService`** (foreground service): owns the engine; **serializes requests**
  (Mutex/Channel — mobile can't run concurrent LLM batches); load-on-demand; persistent
  notification; clean cancellation.

**Single-resident + queue:** one model, one in-flight generation, FIFO queue with
cancellation; a 2nd model load evicts the first. This is how the 12 GB budget is honored.

## 3. The API surface (Ktor, hosted in the service)
- `POST /v1/chat/completions` — OpenAI-compatible, **multimodal** (text + image base64),
  **SSE streaming**.
- `GET /health` — `{model_loaded, model_id, ram_free_mb, busy, version}` (drives the heartbeat).
- `GET /v1/models` — installed / loaded.
- *(later)* `POST /v1/audio/transcriptions`.

**Three consumers, ONE server** (this is the "share one model" decision made concrete):
- **App UI** → in-process / localhost.
- **Keyboard** (separate app) → `http://127.0.0.1:<port>/v1/chat/completions` — localhost,
  same-device trusted. **No AIDL needed**; the keyboard's premium VL mode just hits this.
- **Dove** → `http://<phone-tailnet>:<port>/…` via the box `phone-vision-mcp` shim.

**Bind & auth:** bind `0.0.0.0:<port>`. Localhost = trusted (same device). Non-localhost
(tailnet) **requires `Authorization: Bearer <PHONE_VL_TOKEN>`** (matches the shim env);
reject tailnet requests without it.

## 4. Availability / heartbeat (the graceful-fallback enabler)
- **Pull model:** the box shim health-checks `/health`; unreachable/asleep → Dove falls
  back to Qwen-VL. The phone pushes nothing — simplest.
- **Doze:** foreground service + battery-optimisation exemption keeps it alive when
  feasible; deep-idle Doze still throttles network — **accepted**, the fallback covers it.
- *(optional)* app posts availability to Watchdog for node telemetry.

## 5. Data & persistence
- **Room**: conversations, messages (text + attachment refs), model registry, tool-call log.
- **DataStore**: settings (model defaults, server exposure + token, engine, theme).
- **Files**: model files (app-private), attachment cache.

## 6. UI / UX (feature layer)
- **Chat** — threads + streaming + markdown/code; image & voice attach; **share-sheet
  target** ("share a screenshot → translate/ask").
- **Skills (quick actions)** — one-tap fast-lane: Translate screen · Summarize · OCR ·
  Draft reply · Describe image.
- **Models** — installed, download/import, active + **RAM indicator**, load/unload.
- **Mesh status** — tool-server exposed?, token, Dove reachability, Watchdog reg.
- **Settings** — defaults, engine, exposure, theme.

## 7. Tech stack
- Kotlin · **Jetpack Compose** (single-Activity + Navigation) · **Coroutines/Flow**.
- **Hilt** (DI) · **Room** + **DataStore** · **Ktor** (embedded server + client).
- **LiteRT-LM / MediaPipe LLM Inference** (Gemma); pluggable **MNN / llama.cpp** (Qwen3-VL).
- compose-markdown · CameraX · share-target intent-filter.

## 8. Security
- On-device inference; sensitive data never leaves unless a server engine is chosen.
- Tailnet-only exposure + bearer token; localhost trusted; **no secrets in the APK**
  (token entered in settings, matches the shim).
- Reused keyboard surfaces (overlay/accessibility) keep the sideload restricted-settings caveats.

## 9. Module layout (Gradle)
`:app` (UI/DI/nav) · `:core-inference` · `:core-server` · `:core-data` · `:core-domain`
· `:core-mesh` · feature modules. Start as packages; split to modules as it grows.

## 10. In-app build milestones
- **M1 — Inference service (headless):** `InferenceService` + `LiteRtEngine` + Ktor
  `/v1/chat/completions` + `/health` + token. **Completes SCOPE Phase-2 Step 2 →
  `phone.vision` routes to the phone** (just set the shim's `PHONE_VL_BASE`).
- **M2 — Chat UI:** Compose chat with the local model (streaming).
- **M3 — Multimodal + models:** image attach + share-sheet, model management, RAM indicator.
- **M4 — Skills + voice + mesh:** quick-action skills, voice in, mesh status, Watchdog +
  apps-hub publish.
- **M5 — Upgrade engine:** Qwen3-VL-8B via MNN/llama.cpp behind the same interface.

## 11. Key decisions (this doc)
- Cross-app sharing = **localhost HTTP** (not AIDL) — one server, three consumers.
- **Single-resident model** + serialized requests — RAM-safe.
- **Engine abstraction** from day 1 — Qwen3-VL slots in without UI changes.
- **M1 = headless inference service first** — unblocks Dove + the keyboard before any UI.

## 12. Open forks (owner call — scales the feature layer)
- **App identity/ambition** — Sentinel-node-first (lean) vs full local-assistant (Open-WebUI-parity).
- **First milestone** — M1 headless service vs UI-first chat.
- **Cross-app sharing** — localhost HTTP (rec) vs AIDL.
