# Sentinel Gaming — Automation v1 — design

**Status**: draft (Phase 0 — design doc, no production code yet)
**Owner**: azfar
**Last updated**: 2026-05-27
**Repo**: `sentinel-gaming-automation` (sister to `sentinel-gaming`)
**Related**: `auth-perms-v2.md`, `sentinel-smdl-android` (agent build patterns)

Lock-in document for a real-device bot farm for mobile games — initial
target Lords Mobile, designed to extend to other games via per-game
catalogue files.

---

## 1. Goals

1. **Run 10 game accounts in parallel** on cheap used Android phones,
   each phone hosting one account.
2. **Centralised control plane** that schedules per-account behavior,
   monitors phone health, surfaces ban indicators, and is administered
   from a single web UI integrated into the Sentinel Suite.
3. **Detection-avoidance posture**: real-device fingerprints,
   humanised input timing, behavioral randomisation across accounts,
   no inter-account telemetry leakage.
4. **Game-agnostic design**: Lords Mobile in v1; the same dispatcher
   + agent + behavior-tree pipeline supports any game via per-game
   catalogue YAML.
5. **Independent of `sentinel-gaming`** — distributing a botting
   framework alongside the OSS game-server-hosting product would
   muddy that product's legal positioning. Botting lives in a
   separate, owner-only repo.

## 2. Non-goals

- **Multi-tenancy** — single owner running their own farm. No
  customers, no SaaS, no public signup.
- **Real-money-trading (RMT) ops** — buying/selling game currency
  for real money. Out of scope and ethically distinct from personal
  farm botting.
- **Architecture D protocol-level botting** — out of scope, requires
  1-engineer-year of reverse engineering per game. Architecture C
  (on-device AccessibilityService) is the v1 approach.
- **Cross-account collusion automation** — accounts being aware of
  each other and coordinating attacks. Each account behaves like an
  independent human in v1.
- **Cloud / VPS / emulator deployment** — real phones only. Specifically
  rejected after the architecture analysis (emulator Play Integrity
  fingerprint defeats the security model).
- **Bypassing the game's own anti-cheat infrastructure** — we operate
  WITHIN the spec of what a normal Android user can do (tap, swipe,
  screenshot). We don't hook the game process, read its memory, or
  modify its APK.

## 3. Architecture overview

```
   ┌─ Operator's Sentinel host (existing PC) ───────────────────────────┐
   │                                                                    │
   │  sentinel-gaming-automation/                                       │
   │    dispatcher/  ← Python FastAPI service, port 8210                │
   │      • WebSocket to each agent                                     │
   │      • SQLite per-account state                                    │
   │      • Behavior tree scheduler                                     │
   │      • Anti-correlation timing engine                              │
   │      • Web admin (proxied at suite.your-domain.example.com/automation)     │
   │                                                                    │
   │                 ▲ ╲                                                │
   │                 │  ╲                                               │
   │                 │   ╲   WebSocket (tailscale or LAN)               │
   │                 │    ╲                                             │
   └─────────────────┼─────╲──────────────────────────────────────────  │
                     │      ╲                                           │
   ┌─ Phone rack (10 cheap used Android phones) ─────────────────────  │
   │                                                                    │
   │   ┌─Ph 1 ┐ ┌─Ph 2─┐ ┌─Ph 3─┐ ┌─Ph 4─┐ ...   ┌─Ph 10─┐              │
   │   │acctA│ │acctB │ │acctC │ │acctD │       │acctJ  │              │
   │   │     │ │      │ │      │ │      │       │       │              │
   │   │ SentinelAutomationAgent.apk on each:   │       │              │
   │   │   • AccessibilityService                │       │              │
   │   │   • OpenCV / ML Kit OCR                 │       │              │
   │   │   • Game APK installed (Lords Mobile)   │       │              │
   │   │   • WebSocket to dispatcher             │       │              │
   │   │   • HumanizedInput layer                │       │              │
   │   └─────┘ └──────┘ └──────┘ └──────┘       └───────┘              │
   │                                                                    │
   └───────────────────────────────────────────────────────────────────┘
```

Dispatcher is **Python on the host PC** (could later move to RPi if
ops requires). Agent is **Kotlin Android app** sideloaded onto each
phone. WebSocket bridge over LAN (or Tailscale tailnet — phones
already on tailnet for monitoring).

## 4. Per-account state model

### 4.1. SQLite schema

```sql
CREATE TABLE IF NOT EXISTS accounts (
    id              TEXT PRIMARY KEY,         -- 'alice', 'bob' etc.
    display_name    TEXT NOT NULL,
    game            TEXT NOT NULL,             -- 'lords-mobile' for v1
    phone_id        TEXT NOT NULL,             -- one phone per account in v1
    created_at      TEXT NOT NULL,
    onboard_status  TEXT NOT NULL,             -- 'fresh' | 'soaking' | 'active' | 'paused' | 'banned'
    paused_until    TEXT,                       -- null = not paused
    last_active_at  TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS phones (
    id              TEXT PRIMARY KEY,         -- 'pixel-4a-01'
    model           TEXT,
    android_version TEXT,
    serial          TEXT,                       -- adb serial (when reachable)
    last_seen_at    TEXT,
    battery_pct     INTEGER,
    temp_c          REAL,
    status          TEXT NOT NULL,              -- 'online' | 'offline' | 'overheated' | 'bricked'
    exit_node       TEXT                        -- 'home' | 'sentinel-pia-exit' | ...
);

-- The bot's working memory per account — game state as it knows it
CREATE TABLE IF NOT EXISTS account_state (
    account_id      TEXT PRIMARY KEY REFERENCES accounts(id),
    state_json      TEXT NOT NULL,              -- resources, cooldowns, buildings, troops
    updated_at      TEXT NOT NULL
);

-- One row per action taken — for debugging + per-account behavior
-- learning + ban-pattern analysis
CREATE TABLE IF NOT EXISTS actions_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    action_kind     TEXT NOT NULL,              -- 'collect_resource' | 'train_troops' | 'attack' | ...
    params_json     TEXT,
    result          TEXT,                       -- 'ok' | 'failed' | 'state_changed'
    error           TEXT,
    duration_ms     INTEGER
);

-- Ban indicators and anomaly events
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    account_id      TEXT,
    phone_id        TEXT,
    severity        TEXT NOT NULL,              -- 'info' | 'warn' | 'critical'
    kind            TEXT NOT NULL,              -- 'kicked' | 'captcha' | 'phone_offline' | 'overheated'
    detail          TEXT
);
```

### 4.2. Account lifecycle states

```
fresh ──► soaking ──► active ──► paused ──► active
                       │
                       └─► banned (terminal)
```

- **fresh** — account just created, no manual or bot play yet
- **soaking** — manual play period (2-3 days minimum). Bot is OFF.
  Purpose: account doesn't look statistically obvious (zero-second
  registration → bot session).
- **active** — bot dispatching commands. Normal state.
- **paused** — bot stopped on this account (manually, or auto due
  to alert). `paused_until` is the planned resume time.
- **banned** — game returned ban indicators. Terminal; account
  recorded for post-mortem but never dispatched again.

## 5. Behavior tree

Each account runs a per-game behavior tree (BT). For Lords Mobile v1:

```
RootSelector("LordsMobileBot"):
    ├── Sequence("emergency-defense"):
    │     ├── HasIncomingAttack
    │     ├── HasShieldAvailable
    │     └── ActivateShield
    │
    ├── Sequence("captcha-handling"):
    │     ├── CaptchaDetected
    │     └── AlertOwnerHumanIntervention   ← stops the bot for THIS account
    │
    ├── Sequence("daily-tasks"):
    │     ├── CooldownReady("daily")
    │     └── CollectDailyRewards
    │
    ├── Sequence("collect-resources"):
    │     ├── HasIdleResourceTile
    │     ├── HasAvailableMarch
    │     └── DispatchMarch(target=tile)
    │
    ├── Sequence("train-troops"):
    │     ├── HasIdleTrainingBuilding
    │     ├── HasResourcesForTroops
    │     └── QueueTroopTraining(type=t1 or t4 based on level)
    │
    ├── Sequence("upgrade-buildings"):
    │     ├── HasIdleBuilder
    │     ├── HasUpgradeQueued
    │     └── ExecuteUpgrade
    │
    ├── Sequence("research"):
    │     ├── AcademyIdle
    │     ├── HasResearchPlanned
    │     └── StartResearch
    │
    ├── Sequence("scouting"):
    │     ├── HasIdleMarch
    │     ├── HasUnscoutedTargets
    │     └── ScoutTarget
    │
    └── Action("humanlike-rest"):
          SleepBriefly(jitter=30-120s)
```

Each leaf is a tiny Kotlin function in the agent (Lords-Mobile-specific
UI knowledge) — the orchestration above is Python in the dispatcher.

### 5.1. Selector priority

Top-to-bottom evaluation. **Emergency defense** always runs first.
**Captcha handling** alerts the owner and stops further automation
on that account until manual intervention (any successful captcha
re-enables the bot). The rest are routine-priority and run as
preconditions allow.

### 5.2. Per-game catalogue

Behavior tree is defined per-game in `data/<game>.yaml`. For Lords
Mobile in v1, but extensible:

```yaml
# data/lords-mobile.yaml — game catalogue
game: lords-mobile
package: com.igg.android.lordsmobile
behavior_tree:
  - selector
  - children:
    - sequence: emergency_defense
      ...
ocr_regions:
  food:    [120, 30, 220, 60]   # x1,y1,x2,y2 on 1080×2400 reference
  stone:   [240, 30, 340, 60]
  iron:    [360, 30, 460, 60]
  gold:    [480, 30, 580, 60]
  gems:    [820, 30, 940, 60]
templates_dir: templates/lords-mobile/
ui_tap_zones:
  collect_resource:   [...]
  open_kingdom_map:   [...]
  ...
```

Adding a new game: drop another YAML + take UI screenshots + crop the
button templates. ~1 day per game once the agent supports it.

## 6. Anti-correlation engine

The dispatcher is the central authority on **timing** — agents are
dumb executors. Anti-detection lives in the dispatcher's scheduling
logic:

### 6.1. Per-account personality profile

```yaml
# data/accounts/alice.yaml
account_id: alice
phone_id: pixel-4a-01
timezone: +08:00            # UTC offset for play schedule
play_window:                # "active hours" (UTC offset applied)
  weekdays: [06:30, 22:30]
  weekends: [08:00, 24:00]
breaks:                     # planned downtime within play_window
  - [12:30, 13:45]          # lunch
  - [17:30, 18:30]          # commute
activity_level: aggressive  # 'aggressive' | 'moderate' | 'casual'
risk_tolerance: low         # gem spending preference
nightly_idle: true          # stops between 23:00-06:00
behavior_drift:
  base_action_delay_s: 8        # human-ish base delay between actions
  delay_jitter_s: [-3, +12]      # randomization range
  rest_probability: 0.15         # 15% chance to extra-pause after any action
```

Each of the 10 accounts gets a different profile. Some early-risers,
some night-owls. Different breaks, different aggression levels.

### 6.2. Cross-account scheduling

Dispatcher's scheduler runs a single asyncio loop:
- Iterates accounts every 5 seconds
- For each: check personality profile — is this account "awake" right
  now? If yes, can it act, or is it mid-rest?
- If actable: ask its behavior tree for the next action.
- Dispatch via WebSocket to that account's agent.

Critical anti-correlation rules:
- **Never dispatch the same action across N accounts within X seconds**
  (the same gear collection happening on 5 accounts at the same
  second is a flagged signature)
- **Never have all accounts log in within an hour of each other after
  a daily downtime** — stagger first-login times.
- **Network egress**: rotate Tailscale exit nodes across accounts.
  Some via home IP, some via PIA exit, some via cell tether. Owner
  configures the rotation in `data/exit_rotation.yaml`.

## 7. Agent (Android app) — `SentinelAutomationAgent.apk`

### 7.1. Components

```
Module                Path
─────────────────────────────────────────────────────────────────
AccessibilityService    BotAccessibilityService.kt
ScreenAnalyzer          ScreenAnalyzer.kt
  ├── TemplateMatcher   OpenCV-based (Android NDK)
  ├── OcrExtractor      ML Kit Text Recognition (offline model)
  └── ColorSampler      pixel-region region checks
GameKnowledge           per-game adapter, e.g. LordsMobileGame.kt
  ├── ButtonLocator     tap-zone coords from catalogue YAML
  ├── StateClassifier   "what screen am I on?" FSM input
  └── ActionExecutor    HumanizedTap + Swipe + Wait
WebSocketClient         OkHttp WS, reconnect with backoff
DispatchProtocol        JSON message schema
HeartbeatService        every 30s → battery, temp, last_action
LocalState              Room DB for offline replay if dispatcher down
```

### 7.2. Dispatch protocol (WebSocket JSON)

Dispatcher → agent:

```json
{
  "type": "action",
  "id": "act-bc4d2",
  "kind": "collect_resource",
  "params": {
    "target": {"x": 12345, "y": 67890, "type": "food_tile_lv5"},
    "max_duration_min": 15
  },
  "deadline_ts": 1779870000
}
```

Agent → dispatcher:

```json
{
  "type": "action_result",
  "id": "act-bc4d2",
  "result": "ok",
  "duration_ms": 4831,
  "state_delta": {
    "marches_busy": 3,
    "food_t5_collected": 240000
  }
}
```

Other message kinds:
- `state_snapshot` — agent's full state read (resources, cooldowns)
  reported on tick (~once per minute)
- `alert` — agent-detected anomaly (captcha, "you've been banned"
  modal, crash recovery)
- `heartbeat` — phone health
- `command` — dispatcher → agent admin (pause, screenshot-now, restart,
  game-app-restart)

### 7.3. AccessibilityService permissions

The agent's `AndroidManifest.xml` requests:
- `BIND_ACCESSIBILITY_SERVICE` — for `dispatchGesture` + `takeScreenshot`
- `INTERNET` — WebSocket
- `FOREGROUND_SERVICE` — to survive Android battery killers
- NOT `SYSTEM_ALERT_WINDOW` — no overlays needed
- NOT `WRITE_EXTERNAL_STORAGE` — no file system mutation

Critically: NOT root, NOT Magisk, NOT custom ROM. Stock firmware to
preserve Play Integrity passing.

## 8. Web admin (dispatcher's UI surface)

Suite route: `/automation` (proxied at `suite.your-domain.example.com/automation`).
Owner-only, gated by `gaming.automation.admin` scope.

Pages:
- `/automation` — dashboard overview
  - Live status grid: all phones + accounts at a glance (green/yellow/red)
  - Aggregate stats: actions/hr, resources accumulated, alerts open
- `/automation/accounts/<id>` — per-account detail
  - State snapshot, last 100 actions, behavior-tree decisions
  - Edit personality profile
  - Pause / resume button
- `/automation/phones/<id>` — per-phone detail
  - Health, battery curve, temperature, ADB-reachability
  - Last screenshot (optional — for debugging)
  - Run command (e.g. restart game, take screenshot, reboot)
- `/automation/alerts` — feed of incidents
  - Captchas requiring intervention
  - Phones offline
  - Suspected ban indicators
- `/automation/audit` — full actions log (sortable, filterable)

## 9. Phases

### Phase 0 (this doc, 1 hr) — DONE
Spec locked. Repo scaffolded.

### Phase 1 — Single-account proof of concept (~1 week)
- One phone, one account, no dispatcher
- Agent runs everything locally with a hardcoded behavior tree
- Validates: AccessibilityService permissions, template matching,
  OCR pipeline, ban-detection patterns

### Phase 2 — Dispatcher + multi-phone control (~1 week)
- Python dispatcher on host
- Agent talks WebSocket to dispatcher
- 3 phones, 3 accounts
- Per-account personality profile + behavior tree in dispatcher

### Phase 3 — Scale to 10 phones (~3 days)
- Acquire remaining 7 phones
- Onboard accounts (stagger over 2-3 weeks)
- Wire exit-node rotation

### Phase 4 — Observability + safety nets (~1 week)
- Ban-indicator detection
- Auto-pause-all-bots on critical alert
- Backup account credentials (vault'd)
- Reporting dashboards on suite tile

### Phase 5+ — Multi-game extensibility (future)
- Add a second game (Rise of Kingdoms, Top War, MapleStory M, etc.)
- Per-game catalogue YAML + agent adapter

Total to fully realised v1 (10 accounts on Lords Mobile, central
admin, observability): **~3-4 weeks** of focused part-time work.

## 10. Security considerations

### 10.1. Operator security
- Account credentials stored in WCM (Sentinel's existing secret manager)
- Per-account Google/Facebook account creds rotated through WCM
- Database with account state — only readable from the dispatcher
  process; web admin requires owner cookie

### 10.2. Game-account safety
- Personality profiles enforce humanlike timing — no 24/7 grinding,
  varied per account
- Anti-correlation engine prevents action-pattern fingerprinting
- Network exit rotation prevents IP-cluster detection
- Ban-detection auto-pauses ALL accounts on first alert (prevents
  cascade — if one looks botted, the others might be next in ban
  wave)

### 10.3. Legal / ethical
- This violates Lords Mobile's ToS. The operator (you) accepts the
  risk of account bans on the bot accounts.
- We do not redistribute the agent APK publicly. Distribution =
  facilitation of ToS violation by third parties = different legal
  exposure.
- We do not touch the game's APK, do not patch its binaries, do not
  hook its process. We use only documented Android APIs that a normal
  user has — same APIs an accessibility user (low vision, motor
  impaired) uses.
- We do not sell automation as a service. Single-operator personal
  use.

### 10.4. Phone farm physical security
- Phone rack lives on-premise (azfar's home)
- No external network access to phones (only LAN/tailnet)
- USB-hub power can be cut centrally if a wave of bans hits
- Phones rebooted regularly so memory dumps don't persist account
  state in RAM longer than a session

## 11. What this is NOT and won't become

To prevent feature creep:
- Not a public SaaS. Not "Sentinel Bot Farm as a Service".
- Not multi-tenant. Not "your friends can rent slots on my farm".
- Not RMT (no resource selling for cash).
- Not cross-account collusion (no coordinated attacks orchestrated
  via the dispatcher).
- Not a general "play any game for me" tool. Specifically scope-
  scoped to gather/build/train games with predictable UI flows.

If any of these emerge as want-to-haves, that's a v2 conversation
needing fresh scope analysis.

## 12. Open questions

1. **CAPTCHAs**: when game shows captcha, agent should alert owner
   via push notification (Telegram bot reusing SMDL infra). Owner
   has ~3-5 min to solve before account auto-pauses. Acceptable?

2. **Onboarding new accounts**: do we manual-play each new account
   for 2-3 days, or write an onboarding bot mode that plays
   intentionally-slowly for 72 hours before promoting to normal
   speed?

3. **Game updates**: when the game APK updates, UI coords / button
   templates may shift. Detection strategy: run a "smoke test" each
   morning against captured templates. If smoke test fails on >50%
   of UI elements, pause all bots + alert owner to re-capture
   templates.

4. **Multi-account-per-phone**: switching accounts within a single
   phone via game's "account switch" feature lets us run 2-3
   accounts per device. Cuts hardware cost. Adds switching overhead
   (~30 sec per account flip) and detection risk (same device
   fingerprint → multiple accounts → flagged correlation). v1 says
   1:1; v2 may revisit.

5. **Phone failure recovery**: when a phone bricks, do we
   auto-reprovision a hot spare, or just keep that account paused
   until manual intervention? v1 says manual; automation here is
   over-engineering.

## 13. Decision log

- **Sister repo, not subdirectory of sentinel-gaming**: keep the
  legal grey product separate from the legal clean OSS product.
- **Architecture C, not D or emulator**: detection-avoidance + cost
  analysis from the prior conversation — Architecture C with real
  phones is economically + risk-wise optimal at the 10-account scale.
- **Python dispatcher, Kotlin agent**: dispatcher is server-style
  code (FastAPI, asyncio, fits the rest of the Sentinel stack);
  agent is on-device (Kotlin, AccessibilityService, native Android).
- **WebSocket bidirectional, not polling**: real-time alerts (captcha,
  ban indicator) need server push; HTTP polling adds latency.
- **SQLite, not Postgres**: single-tenant, single-host. Postgres is
  overkill.
- **No agent auto-update**: the dispatcher tells you when the agent
  needs to be re-sideloaded. Auto-update over WebSocket adds attack
  surface and complexity not justified at this scale.

---

**Sign-off**: this is the contract for Sentinel Gaming Automation v1.
Phase 1 implementation will conform to §7; Phase 2 to §3 + §4 + §6;
etc. Updates require an edit here + a note in §14.

## 14. Changelog

- 2026-05-27 — initial draft (Phase 0). azfar.
