# Sentinel VPN — Architecture (refined intent, 2026-05-11)

Four zones, three egress paths, one policy layer.

---

## The model in one frame

```
        PERSONAS                       MESH / TUNNEL                EGRESS
                                                                              
   ┌──────────────────┐         ┌──────────────────────────┐                  
   │  You             │         │  Zone A — OWNER          │                  
   │  (phone, laptop, │ ──tag───┤  Headscale (or Tailscale)│                  
   │   anywhere)      │         │  tag: owner              │                  
   └──────────────────┘         └──────────┬───────────────┘                  
                                           │                                  
                                           ▼                                  
                                ┌────────────────────────┐                    
                                │ POLICY: exit via PIA   │ ──► PIA SaaS ──┐  
                                │ (privacy, geo-unlock)  │   (dedicated IP)│  
                                └────────────────────────┘                 │  
                                                                           │  
   ┌──────────────────┐         ┌──────────────────────────┐               │  
   │  Gamers          │         │  Zone B — LAN GAMING     │               │  
   │  (ARK players,   │ ──tag───┤  Headscale (or direct)   │               │  
   │   on-LAN + remote│         │  tag: gaming             │               │  
   │   friends)       │         │  port-forward: ARK 7777  │               │  
   └──────────────────┘         └──────────┬───────────────┘               │  
                                           │                                │  
                                           ▼                                │  
                                ┌────────────────────────┐                  │  
                                │ POLICY: exit via home  │ ──► VIEWQWEST ──┤  
                                │ (low latency direct)   │   (residential IP)│
                                └────────────────────────┘                  │  
                                                                           │  
   ┌──────────────────┐         ┌──────────────────────────┐               │  
   │  Public visitors │         │  Zone C — CF TUNNEL      │               │  
   │  (mini-app TOTP, │ ──http──┤  Cloudflare (no mesh)    │               │  
   │   media share)   │         │  *.your-domain.example.com       │               │  
   └──────────────────┘         └──────────┬───────────────┘               │  
                                           │                                │  
                                           ▼                                │  
                                ┌────────────────────────┐                  │  
                                │ POLICY: loopback only  │                  │  
                                │ (inbound only,         │                  │  
                                │  no egress relevant)   │                  │  
                                └────────────────────────┘                  │  
                                                                           │  
   ┌──────────────────┐         ┌──────────────────────────┐               │  
   │  Friend (Russia) │         │  Zone D — AMNEZIAWG      │               │  
   │  RKN-blocked     │ ──UDP───┤  Parallel VPN (no mesh)  │               │  
   │  region          │  51234  │  10.20.0.0/24            │               │  
   └──────────────────┘         └──────────┬───────────────┘               │  
                                           │                                │  
                                           ▼                                │  
                                ┌────────────────────────┐                  │  
                                │ POLICY: latency-aware  │ ──► PIA ────────┤  
                                │ • PIA for EU/US dest   │     (if better) │  
                                │ • Home for SG dest     │ ──► VIEWQWEST ──┤  
                                │ • LAN reach: REJECTed  │     (if better) │  
                                └────────────────────────┘                  │  
                                                                           │  
                                                                           ▼  
                                                                       INTERNET
```

---

## Four zones, one summary table

| Zone | Mesh / Tunnel | Members | Default egress | Can reach LAN? | Reaches you via |
|---|---|---|---|---|---|
| **A — Owner** | Headscale-or-Tailscale `tag:owner` | Your phone, laptop, any device you trust | **PIA SaaS exit** (dedicated IP, privacy, geo-unlock) | ✅ Full | tailnet IP `100.x.x.x` |
| **B — LAN Gaming** | Headscale `tag:gaming` OR direct LAN/port-forward | ARK players (you + invited friends) | **Home WAN** (direct, low latency) | ✅ Only ARK + game-relevant services | tailnet IP OR direct IP+port |
| **C — CF Tunnel** | Cloudflare Tunnel (no mesh) | Anyone with a valid URL | n/a (inbound only) | ❌ Loopback ports only | `*.your-domain.example.com` |
| **D — AmneziaWG** | Parallel WireGuard with obfuscation | Friend in Russia (RKN-bypass) | **PIA OR Home** (latency-driven, per-destination) | ❌ REJECTed by iptables | UDP 51234 on home IP |

---

## Where PIA fits

```
                      ┌──────────────────────────────────────────┐
                      │  gluetun container (recommended)         │
                      │  ├── reads PIA WireGuard config         │
                      │  ├── exposes a tunnel device (tun0)     │
                      │  └── advertises itself in the mesh as   │
                      │      an EXIT NODE (Tailscale/Headscale  │
                      │      "advertise-exit-node" flag)        │
                      └────────────────┬─────────────────────────┘
                                       │
                                       ▼
                        Mesh peers tagged `tag:owner` enable
                        "use exit node = sentinel-pia-exit" →
                        all their traffic leaves via PIA.

                        Peers tagged `tag:gaming` do NOT enable
                        the exit node → their traffic leaves via
                        the host's normal eth0 (home WAN).
```

This is **per-peer choice** in Tailscale's model. The exit node exists; nodes opt in via their own settings. ACL rules can also enforce "tag:owner MUST use exit node X" if you want it non-bypassable.

For Zone D (AmneziaWG), gluetun's tunnel device gets a second route entry — iptables marks packets from `10.20.0.x` and `ip rule` steers them via a second routing table that exits either through PIA or home WAN based on destination.

---

## What changes vs today

| Component | Today | Refined intent |
|---|---|---|
| Mesh control plane | Tailscale SaaS (only) | **Headscale or Tailscale — abstracted**. Whichever is chosen, multi-tag ACL is the unit of policy. If Tailscale stays, the tag/ACL work happens at the Tailscale admin console. If Headscale revives, same model. |
| Egress | Single path: home WAN | **Three paths: PIA / Home / loopback**, selected by tag |
| PIA subscription | Not yet | **NEW**: PIA dedicated IP subscription (~$5-10/mo). Container: `qmcgaw/gluetun` configured with PIA WireGuard credentials. Advertise-exit-node enabled. |
| LAN gaming | ARK server reached via direct LAN or port-forward | **Tagged**: ARK server peers join the mesh under `tag:gaming` so policy is explicit even when also publicly reachable on port 7777 |
| AmneziaWG egress | MASQUERADE via eth0 (home WAN only) | **Policy-routed**: source-IP based steering of friend traffic to PIA or home WAN per destination latency |
| Cloudflare Tunnel | Public ingress (unchanged) | Unchanged — already correct |

---

## Trust boundaries (unchanged structure, refined labels)

```
┌─────────────────────────────────────────────────────────────────────┐
│  TIER A — Zone A (tag:owner)                                        │
│   Full host + LAN + mesh, exit identity = PIA dedicated IP         │
│   Defence: mesh auth (Headscale/Tailscale ACL)                      │
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│  TIER B — Zone B (tag:gaming)                                       │
│   ARK + game services only, exit = home IP (for low latency)       │
│   Defence: ACL rule "tag:gaming → only ports 7777, 27015, etc."   │
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│  TIER C — Zone C (Cloudflare Tunnel, no mesh)                       │
│   Mini-app TOTP-gated + media HMAC-signed share URLs (24h)         │
│   Defence: TOTP, HMAC, Cloudflare WAF                              │
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│  TIER D — Zone D (AmneziaWG, parallel)                              │
│   Internet egress only, LAN REJECTed, exit = PIA or Home per route │
│   Defence: per-peer keys + iptables REJECT-LAN + AmneziaWG         │
│            obfuscation (RKN-bypass)                                 │
└─────────────────────────────────────────────────────────────────────┘
```

The asymmetries that matter:
- **Tier A**, despite full LAN access, **exits via PIA** — so geo-unlock + privacy when traveling
- **Tier B** stays on home IP — because gamers need every millisecond of latency they can save
- **Tier D** can pick either egress per destination — friend gets best-path routing without LAN exposure

---

## Why this layout is "more sensible"

Three reasons the old one was muddled and this one isn't:

1. **Old**: one mesh, one egress (home WAN), with AmneziaWG bolted on. Egress policy was implicit. ⇒ **New**: egress is a **first-class architectural element** with three explicit paths and a policy layer on top.

2. **Old**: gaming server traffic mixed with personal traffic. ⇒ **New**: `tag:gaming` is its own zone — different ACL surface, easy to add/remove kids' devices without expanding owner's mesh trust.

3. **Old**: Headscale was "deployed and parked" because of Android UX gates on the user side. ⇒ **New**: control-plane choice (Headscale vs Tailscale SaaS) is abstracted — the architecture works either way. Pick whichever has the better Android UX at decision time.

---

## Implementation path

Phased — each step is independently shippable.

| Phase | Scope | Effort | Trigger |
|---|---|---|---|
| **0 — Today** | Single egress, Tailscale SaaS, AmneziaWG side-channel | ✓ done | — |
| **1 — Tags + ACL** | Define `tag:owner` and `tag:gaming` on the mesh (Tailscale SaaS or Headscale). Tag your existing devices. Write the ACL rules. No new infra. | ~1 h | Anytime — pure config |
| **2 — PIA exit node** | **Provider locked: PIA (user already has dedicated IP).** Stand up gluetun + tailscale-pia sidecar in docker-compose under `profile: vpn`. Connect to mesh as advertised exit node. | ~1.5 h | 🟡 **SCAFFOLDED** — compose services + credential-rotation script committed. Awaiting: user runs `scripts/rotate_pia_creds.ps1` + generates Tailscale auth key. Then `docker compose --profile vpn up -d pia-exit tailscale-pia`. |
| **3 — Owner devices use exit** | Enable "use exit node" on your phone + laptop Tailscale clients (or Headscale equivalent). | ~10 min | After Phase 2 |
| **4 — AmneziaWG policy routing** | Add `ip rule` + second routing table so AmneziaWG client traffic can take either egress per destination. Latency-test PIA vs home for SG, EU, US destinations and bake the best-default into the routing table. | ~2 h | After Phase 2 (needs gluetun) |
| **5 — Gaming zone ACL** | Carve gaming devices into `tag:gaming` with restricted ACL (game ports only, no general LAN access). Confirm ARK stays reachable. | ~1 h | When non-owner gamers join the mesh |
| **6 — Carve out `sentinel-vpn`** | Sanitize templates (`headscale-config/config.yaml`, `amneziawg-config/awg0.conf` minus keys, gluetun compose stanza, ip-rule recipes, ACL.json). Push to `github.com/YOUR_GITHUB_USERNAME/sentinel-vpn`. | ~3 h | After phases 1-4 stable for 1-2 weeks |

Total to fully refined: ~9 h spread across whenever you have time. Phase 1 gets you 80% of the conceptual win for ~1 h of work.

---

## Open questions (refined)

| Q | What to weigh |
|---|---|
| **PIA vs Mullvad vs ProtonVPN?** | PIA: dedicated-IP option, US-based (legal risk for some). Mullvad: cash payment, no email required, more privacy theatre. ProtonVPN: Swiss, EU-friendly. Pick by jurisdiction + dedicated-IP availability. |
| **Headscale revival NOW or later?** | If Phase 1+2 work fine on Tailscale SaaS, no rush. Revisit when (a) Tailscale Inc privacy becomes a real concern, OR (b) the mesh exceeds 3 users (their free tier cap). |
| **Should Zone B (gaming) be on the mesh at all, or just LAN + port-forward?** | Mesh: lets remote friends pre-authenticate before they hit ARK, ACL-enforceable. Port-forward only: simpler, but ARK auth is the only gate. |
| **AmneziaWG-via-PIA legality** | Some PIA terms-of-service may restrict using their tunnel to relay other VPN traffic. Check before Phase 4. Alternative: friend gets a separate PIA seat. |
| **Self-hosted DERP?** | Lower latency for mesh + cuts Tailscale Inc's metadata visibility further. Only matters at scale or with active privacy threat. Defer. |

---

## How this lives in the broader stack

- The `OVERVIEW.md` taxonomy still puts VPN under **Tier 3 (Standalone)** — each piece independently usable
- A future `sentinel-vpn` repo carries: Headscale config templates, AmneziaWG profile, gluetun compose stanza, the ip-rule recipes, and a walkthrough that talks an operator through Phase 0 → Phase 5 in their own homelab
- Cross-component touches:
  - SMDL's tailnet file-delivery (path-2) routes via Zone A — owner reaches their own files at low latency despite exiting through PIA for everything else (LAN traffic stays inside the mesh)
  - Mini-app remains Zone C (public via CF Tunnel + TOTP) — unchanged
  - Friend's media-sharing question from the previous draft becomes: add a curated `media.your-domain.example.com` route accessible via Zone D, with an ACL ALLOW just for that one route (rather than full LAN ACCEPT)

---

*Drift policy: when an actual change ships (PIA subscription bought, gluetun deployed, tag created), bump the date and patch the relevant phase row.*
