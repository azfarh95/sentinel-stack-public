# SMDL Projector v1 — design

**Status**: draft (Phase 0 — design doc, no code yet)
**Owner**: azfar
**Last updated**: 2026-05-27
**Related**: SMDL Media tile, sentinel-shared-brain (signaling), Onn 4K Pro / RPi receiver host

Wireless-display sender + receiver pair that replaces Miracast for the
Sentinel stack — phone / Windows laptop / MagicOS desktop casts its
screen to a Sentinel-managed receiver running on a dongle, mini PC,
or Raspberry Pi, which outputs to a projector or TV.

---

## 1. Goals

1. **Cast from existing OS senders** — Windows Wireless Display (Win+K),
   Honor MagicOS desktop ("Cast / Easy Projection"), Samsung Smart View,
   Xiaomi ScreenCast — without installing anything on the sender.
2. **Cast from Sentinel-native apps** — a new SMDL Cast Android app +
   "cast this tab" browser button (long-tail use cases not covered by
   stock OS senders, e.g. AOSP phones post-Miracast removal).
3. **Same-network deployment** — receiver discoverable on the LAN
   without internet access (mDNS / SSDP).
4. **Optional Wi-Fi hotspot mode** — receiver creates its own Wi-Fi
   network, sender connects directly. Useful for hotel rooms,
   conference rooms, and the "my router went down but I want to cast"
   case.
5. **Cheap hardware** — runs on a $35 dongle / RPi 4 / any old laptop.
   No proprietary HDMI dongles needed.

## 2. Non-goals

- **AirPlay (Apple) compatibility** — iOS senders out of scope. Apple's
  protocols are encrypted with hardware-specific keys; the
  `OpenAirplayMirroring` projects exist but are flaky and Apple
  routinely breaks them with iOS updates.
- **Chromecast (Google Cast) source compatibility** — if you can cast
  via Chromecast, you don't need Miracast. Out of scope.
- **DRM-protected content** — Netflix/Disney+ refuse to cast their
  HDCP-protected streams via any open Miracast sink. We won't fight
  this. Cast Sentinel apps (Finance / AI / IPTV) and arbitrary
  desktop content — yes. Netflix — no.
- **HDR / 4K@60Hz** — v1 targets 1080p@30fps. 4K HDR needs HDMI 2.1
  HW decode + tight pipeline tuning; deferred.
- **Multi-screen / video wall** — single sender → single receiver
  mapping in v1. Many → one or one → many deferred to Phase 4.

## 3. The honest cost-benefit framing

Miracast is **hard**. Microsoft's MS-MICE protocol is documented
(MS-MICE.pdf, 130 pages); the Wi-Fi Alliance's underlying Miracast
spec is dense; H.264 RTP packing has quirks; HDCP key exchange (when
needed) is patented. The reference open-source implementation —
**MiracleCast on Linux** — still has open issues like "won't connect
from Win11 with anniversary update" four years after it was filed.

**Realistic effort**:

| Target | Effort | Reliability ceiling |
|---|---|---|
| WebRTC-based Sentinel-only cast (custom protocol) | 1 week | 99% (we control both ends) |
| Miracast sink compatible with Windows | 2-3 weeks | 80-90% (Microsoft moves the goalposts) |
| Miracast sink compatible with Honor MagicOS | +1 week | 70-80% (MagicOS variant has quirks) |
| Wi-Fi P2P hotspot mode (Wi-Fi Direct Miracast) | +1 week | 75% (driver/regulatory issues per region) |
| Full v1 fully compatible with all targets | **4-6 weeks** | mid-80s overall |

If the actual goal is "cast my Sentinel content to my projector",
**v1 phase 1 alone delivers it** and is the highest-ROI piece. The
Miracast-OS-sender compatibility is a hard problem worth doing only
if you'll cast non-Sentinel content (PowerPoint, Slack screenshare,
games, etc.) regularly.

We'll spec all phases and pick which to build at each checkpoint.

## 4. Architecture overview

```
   ┌─ Senders (existing OS) ────────────┐
   │  • Windows 10/11   (Win+K → Cast)   │
   │  • Honor MagicOS desktop (PC Mode)  │
   │  • Samsung Smart View               │
   │  • Xiaomi ScreenCast                │
   │  • Wi-Fi Direct Miracast clients    │
   │  All speak Miracast / MS-MICE       │
   └──────────────────┬──────────────────┘
                      │ TCP 7236 (control)
                      │ UDP RTP (video/audio)
                      ▼
   ┌─ Sentinel Projector receiver ──────────────────────┐
   │                                                     │
   │  ┌─ Discovery layer ──────────────────────────┐     │
   │  │  • mDNS  _miracast._tcp / _display._tcp   │     │
   │  │  • SSDP  urn:wfa-org:device:WFD:1         │     │
   │  │  • Wi-Fi P2P advertisement (hotspot mode) │     │
   │  └────────────────────────────────────────────┘     │
   │                                                     │
   │  ┌─ MS-MICE handshake (TCP/7236) ────────────┐     │
   │  │  Capability exchange, RTSP session setup, │     │
   │  │  optional PIN pairing                     │     │
   │  └───────────────────────────────────────────┘     │
   │                                                     │
   │  ┌─ Stream demux + decode ───────────────────┐     │
   │  │  H.264 video → HW decoder → framebuffer   │     │
   │  │  AAC audio → ALSA / PulseAudio / WASAPI   │     │
   │  └───────────────────────────────────────────┘     │
   │                                                     │
   │  ┌─ Display output ──────────────────────────┐     │
   │  │  HDMI fullscreen via GStreamer / direct DRM│     │
   │  └───────────────────────────────────────────┘     │
   │                                                     │
   │  ┌─ Web admin (suite.your-domain.example.com/projector) ┐  │
   │  │  Sessions list, PIN display, accept/deny     │  │
   │  │  Bitrate / codec / latency stats             │  │
   │  └──────────────────────────────────────────────┘  │
   └─────────────────────────────────────────────────────┘
                      ▲
                      │ HDMI
                      ▼
                  ┌──────────┐
                  │ Projector │
                  └──────────┘
```

## 5. Protocol choices

### 5.1. Miracast (MS-MICE variant) — OS-sender compat

**Compatible senders**: Windows 10/11, Honor MagicOS, Samsung, Xiaomi,
LG TVs (sending to other displays), most "Cast" buttons across Android
12+ OEMs.

**Transport**:
- TCP/7236 — control channel (capability exchange, session setup)
- RTSP over TCP — session negotiation
- RTP/UDP — video (H.264) + audio (LPCM or AAC)
- Optional HDCP 2.2 over RTSP — for DRM content (we punt on this)

**Discovery**: mDNS + SSDP. Receiver advertises:
- `_display._tcp` (mDNS) with TXT records (sink port, name, role)
- SSDP `urn:wfa-org:device:WFD:1` for legacy/Win10 fallback

**Pairing**:
- Optional 8-digit PIN displayed on the receiver's HDMI output
- Sender prompts user to enter PIN (Win+K shows the field)
- After first pair, "trusted devices" stored in `~/.sentinel-projector/trusted.json`

### 5.2. WebRTC — Sentinel-native + browser sender

**Compatible senders**: anything with a modern browser. We'd ship:
- A "Cast this tab" extension / bookmarklet on the Suite
- An Android sender app using `MediaProjection` + WebRTC peer

**Transport**:
- WebRTC peer connection (SCTP for data, SRTP for media)
- Signaling via the existing `sentinel-shared-brain` bridge (already
  WebSocket-pub/sub-able)
- STUN: own host (LAN-only, NAT not relevant)

**Discovery**: a "Receivers" list pulled from the suite — receivers
register themselves at startup; senders pick from the list.

**Pairing**: same Sentinel auth cookie (owner / scoped beta user with
`projector.cast` scope). No PIN needed — the auth is the security.

### 5.3. Wi-Fi P2P / hotspot — Phase 3

Receiver creates a Wi-Fi P2P group:
- Linux: `wpa_supplicant` with `p2p_group_add`
- Configures itself as a "GO" (Group Owner) so senders connect
- Senders connect via standard Wi-Fi Direct Miracast (Windows /
  Samsung / Honor all have this in their cast menus)
- IP allocation via internal DHCP server (`dnsmasq`)

Use case: "the venue's Wi-Fi is bad / I'm in a hotel". Receiver runs
its own Wi-Fi network without needing the venue's router.

## 6. Hardware footprint

### Receiver options

| Host | Cost | Pros | Cons |
|---|---|---|---|
| **Raspberry Pi 4 (4 GB)** | ~S$70 | Linux full control, Wi-Fi P2P works, HW H.264 | 1080p ceiling, audio jitter under load |
| **Onn 4K Pro Streaming Box** | ~S$40 | Android = native MediaCodec; SMDL stack already runs here | Wi-Fi P2P harder on stock AndroidTV; need root |
| **Intel NUC / Mini PC** | S$200+ | 4K capable, real Linux | Overkill cost-wise |
| **Old laptop with HDMI-out** | S$0 (existing) | Free, capable | Power-hungry, bulky, ugly |

**Pick for v1: Raspberry Pi 4 or Onn 4K Pro**. RPi gives Linux flexibility for Wi-Fi P2P; Onn gives easier integration with the existing Sentinel stack.

### Sender requirements

- Windows 10 Anniversary Update (2016) or later — has Miracast SRC built-in
- MagicOS 4.x or later — has Easy Projection
- Samsung One UI 1.0+ — Smart View baked in
- Android phones generally: still hit-or-miss post-Android 9

## 7. Discovery details

### 7.1. mDNS records (Bonjour/Avahi)

```
_display._tcp:
   port: 7236
   txt:  txtvers=1, model=SMDL-Projector, role=sink, fmt=H.264
_workstation._tcp (optional):  also advertise as "regular" Bonjour host
```

### 7.2. SSDP advertisement (UPnP)

```
NOTIFY * HTTP/1.1
HOST: 239.255.255.250:1900
NT: urn:wfa-org:device:WFD:1
NTS: ssdp:alive
USN: uuid:<receiver-uuid>::urn:wfa-org:device:WFD:1
LOCATION: http://<receiver-ip>:7236/description.xml
CACHE-CONTROL: max-age=1800
```

Refresh every 15 min. Sender's `Win+K` browses both mDNS + SSDP and merges results.

### 7.3. Wi-Fi P2P announcement (hotspot mode)

```
P2P_GO_NEG_REQ
  P2P_attribute: device_name = "SMDL Projector"
  WFD_attribute: device_info = sink_supported, audio_supported
```

## 8. Web admin

A new `/projector` route on the Suite (sentinel-vpn-dashboard) showing:

```
┌─ SMDL Projector ─────────────────────────────┐
│                                              │
│  ⚫ no active session                        │
│  ├─ Receiver IP:  192.168.1.50               │
│  ├─ HDMI output:  1920×1080@30Hz             │
│  ├─ PIN:          ████████  (regenerate)     │
│  └─ Hotspot:      off  [toggle]              │
│                                              │
│  Trusted devices:                            │
│  ├─ azfar's laptop (DESKTOP-ABC123)  · forget│
│  └─ azfar's phone (HONOR-MAGIC7-PRO)         │
│                                              │
│  Last 10 sessions:                           │
│  ┌────────────────────────────────────────┐  │
│  │ 2026-05-27 14:32  laptop  1080p  18m   │  │
│  │ 2026-05-27 11:05  phone   720p   3m    │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
```

Auth: scoped under new `projector.view` (read) + `projector.admin`
(toggle/forget). Owner gets `*` (everything).

## 9. Schema

### 9.1. SQLite (in receiver host)

```sql
CREATE TABLE IF NOT EXISTS projector_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    device_name   TEXT,
    device_id     TEXT,                -- mac / signature
    protocol      TEXT NOT NULL,        -- 'miracast' | 'webrtc'
    resolution    TEXT,                 -- '1920x1080@30'
    codec         TEXT,                 -- 'h264 baseline 4.0'
    avg_bitrate   INTEGER,               -- kbps
    peak_latency  INTEGER,               -- ms
    end_reason    TEXT                   -- 'user_stop' | 'timeout' | 'error'
);

CREATE TABLE IF NOT EXISTS projector_trusted (
    device_id     TEXT PRIMARY KEY,
    device_name   TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    auto_accept   INTEGER NOT NULL DEFAULT 0
);
```

## 10. Phases

| Phase | Effort | Ships | Sender compat |
|---|---|---|---|
| 0 | 1 hr | This doc | — |
| 1 | 1 week | Sentinel-native WebRTC receiver + sender (Android app + browser) | SMDL apps only |
| 2 | 2-3 weeks | Miracast/MS-MICE sink (LAN mDNS+SSDP discovery + handshake + H.264 RTP receive + decode/display) | Windows, MagicOS, Samsung, Xiaomi |
| 3 | 1 week | Wi-Fi P2P hotspot mode | + Wi-Fi-Direct Miracast (legacy / hotel rooms) |
| 4 | future | Multi-receiver matrix, audio-only mode, casting from desktop browsers, persistence | full Stremio-style "pick a receiver" UX |

Total to fully-realised v1: **~5-6 weeks** of focused work. Phase 1
alone delivers the "cast Sentinel content to my projector" loop in
about a week.

## 11. Phase 1 detailed plan (the one we should build first)

### 11.1. Receiver — `sentinel-projector` container/app

A small service running on the dongle / RPi / mini PC:

```
sentinel-projector/
├─ Dockerfile               # python:3.12-slim + ffmpeg + gstreamer
├─ app/
│  ├─ main.py               # FastAPI on :7237 (matches MS-MICE +1)
│  ├─ webrtc.py             # aiortc peer connection handling
│  ├─ display.py            # GStreamer pipeline → HDMI
│  ├─ signaling.py          # WS client to sentinel-shared-brain
│  └─ web.py                # /admin pages + status JSON
├─ requirements.txt         # fastapi, uvicorn, aiortc, aiohttp
└─ systemd/
   └─ sentinel-projector.service
```

Bind-mount `/dev/dri` for GPU access, `--network=host` for mDNS.

### 11.2. Senders (multi-form-factor)

**Android sender app** (`sentinel-cast-sender`):
- Same APK build pattern as SMDL IPTV (debug keystore, ship via /apps)
- `MediaProjection` → `MediaCodec` H.264 → WebRTC `addTrack` → peer
- Discovery: `/api/projector/receivers` on the suite returns known receivers
- One-tap "cast to <name>"

**Browser sender** (suite page `/cast`):
- `getDisplayMedia()` for tab/window capture
- WebRTC peer to selected receiver
- No install needed — works from any modern browser

### 11.3. Signaling via sentinel-shared-brain

Re-use the existing WebSocket bridge:
- Receiver subscribes to channel `projector.<receiver-id>`
- Sender publishes `offer` → bridge fans to receiver
- Receiver responds with `answer` → bridge to sender
- ICE candidates relayed similarly

Zero new infrastructure — `sentinel-shared-brain` is already running.

### 11.4. Auth

- Receiver registers with the suite at startup → owner's session cookie
- Sender (Android app) requires the same auth cookie or a v2 scoped
  cookie with `projector.cast` scope
- Receiver verifies the cookie before accepting WebRTC offers

## 12. Phase 2 detailed plan (Miracast OS-sender compat)

### 12.1. Approach: fork MiracleCast or write from scratch?

**Fork MiracleCast** (saves weeks):
- C codebase, GPL, Linux-only
- Mature mDNS/SSDP/RTSP code
- Known Win11 issues but pinpointed bug fixes are tractable
- Best for RPi receiver

**Write from scratch in Python** (cleaner long-term):
- aiortc covers RTP basics
- gst-python for GStreamer pipeline
- 2-3x slower to build but easier to debug + integrate with rest of Sentinel
- Best if we want Suite-pillar integration

Pick: **start with fork**, swap to Python implementation once the
protocol is stable enough to maintain.

### 12.2. The gnarly bits

Things that will eat days:

1. **HDCP key exchange** — we punt; means DRM-content streams won't
   work. Sender will refuse with "this content can't be cast".
2. **Audio sync** — RTP timestamps + buffer management. Pi 4's GPU
   decoder has known A/V sync drift; needs an LMP filter.
3. **Windows-specific quirks** — Win11 24H2 sends slightly different
   capability descriptors than Win10. Conditionals.
4. **Honor MagicOS variants** — they wrap Miracast in their "Easy
   Projection" branding; some versions use proprietary control
   messages. May need protocol sniffing on the actual device.
5. **Bitrate negotiation** — sender wants 25 Mbps, receiver wants
   8 Mbps; what's the handshake. Documented but easy to get wrong.

## 13. Phase 3 detailed plan (Wi-Fi hotspot)

### 13.1. Linux receiver

```bash
# Become the access point
wpa_supplicant -i wlan0 -c projector-p2p.conf

# Become DHCP for the P2P clients
dnsmasq --interface=wlan0 --dhcp-range=192.168.49.10,192.168.49.50,12h
```

The Pi can be on Ethernet (or a USB Wi-Fi dongle) for internet uplink
while wlan0 is dedicated to P2P. Two-NIC setup.

### 13.2. Android receiver

`WifiP2pManager.createGroup()` — works out of the box on Android 4.0+.
The Onn 4K Pro can host a P2P group; AndroidTV variant might need a
non-AndroidTV launcher app to access the P2P APIs.

### 13.3. The catch

Wi-Fi regulatory: many regions cap P2P-group power at low levels (so
your receiver-AP has weak range — works in same room, less reliable
across walls). Hotel rooms = fine. Living-room-to-bedroom = maybe
needs the receiver placed centrally.

## 14. Open questions

1. **Audio routing** — receiver outputs over HDMI by default. Should
   we also support routing audio to a separate sink (e.g. Bluetooth
   speaker, Sonos)? Out of v1 scope but the abstraction can be in the
   GStreamer pipeline.

2. **Multi-resolution senders** — laptop sends 1080p, phone sends
   720p portrait, MagicOS desktop sends 2560×1440. Receiver resamples
   to HDMI output resolution. v1: simple resample. v2: explicit user
   choice ("pillarbox vs scale-and-crop").

3. **Co-existence with Sentinel IPTV** — if you're watching IPTV on
   the dongle, casting interrupts it. Sane behaviour. Should the
   receiver SHOW the IPTV stream to the casting user (so they can
   demo)? Future thought.

4. **Multiple receivers** — same network, multiple Pi receivers in
   different rooms. Phase 4 wants "pick a receiver" UI; v1 assumes
   one.

5. **Internet-less operation** — the suite-based signaling path
   requires reaching `your-domain.example.com` for the WS bridge.
   For hotspot mode this won't work. Need a local-only fallback
   (mDNS-discovered receiver + LAN-only signaling). Out of v1, in
   v1.1.

6. **DRM-protected content** — see §2 non-goals. Will eventually need
   to support some form of HDCP for Netflix/Disney+/etc. on smart
   TVs. Genuinely hard, deferred until there's a real ask.

## 15. Decision log

- **Two-protocol approach (Miracast + WebRTC)**: each protocol handles
  a different sender population. Trying to make Miracast work for
  Sentinel-native apps (no OS sender available) would mean
  reimplementing the SRC side too — way more work than just running
  WebRTC there.

- **Receiver is a separate service / device, not just an Onn app**:
  the Sentinel stack is centralised on the Win11 host today; a
  dedicated receiver gives reliable HDMI output without depending on
  the desktop being on. Phase 4 could collapse this back if desired.

- **No HDCP in v1**: too hard, low real-world value (most content
  worth casting isn't DRM'd).

- **mDNS over SSDP for discovery**: mDNS is the modern standard.
  SSDP advertisement is added only for Windows < 11 / legacy devices.

- **PIN pairing on first connection**: Miracast convention. Sentinel
  cookie auth is required ON TOP for our own apps (Phase 1), so the
  PIN is for the Phase 2 OS-sender case only.

---

**Sign-off**: this is the contract for SMDL Projector v1. Phase 1
implementation will conform to §11; Phase 2 to §12; etc.

## 16. Changelog

- 2026-05-27 — initial draft (Phase 0). azfar.
