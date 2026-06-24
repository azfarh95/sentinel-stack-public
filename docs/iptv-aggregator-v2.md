# IPTV aggregator v2 — design

**Status**: draft (Phase 0 — design doc, no code yet)
**Owner**: azfar
**Last updated**: 2026-05-27
**Related**: `app/iptv.py`, `app/iptv_routes.py`, `app/iptv_youtube.py`

Lock-in document for the Stremio-style aggregator that consolidates
the N source-per-channel sprawl (11.6k rows, ~6k unique logical
channels) into a clean **logical channel → multiple sources** model.

---

## 1. Goals

1. **One card per channel** in the IPTV browse grid. "CNA" appears
   once, not four times.
2. **Auto-pick alive source** on play. If iptv-org's cloudfront URL
   is geo-blocked from the user's exit, fall back to the YouTube
   live mirror. User shouldn't have to know which to click.
3. **Per-source visibility** when wanted. The play page shows all
   known sources; user can manually pin/switch (e.g. "I prefer the
   YouTube one because hls.js plays cleaner").
4. **Cheap dedup pipeline**. No human curation required for the
   common case — algorithmic merge by (normalised name + country)
   + tvg-id family handles >80% of the catalogue. Curated YAML
   covers edge cases.
5. **Backwards-compatible rollout**. Old `/api/iptv/channels` and
   `/iptv/play/<source-prefixed-id>` URLs keep working through
   Phase 1 + 2; deprecation in Phase 3.

## 2. Non-goals

- **Stremio addon protocol compatibility** — different scope. We're
  using the *idea* (catalogue + sources) not the wire format.
- **Channel-quality scoring** — alive/dead is the only signal in v2.
  Bitrate, codec, packet-loss, "feels laggy" deferred.
- **User-contributed sources** — owner-only catalogue. No public
  add-this-channel endpoint.
- **Cross-language deduplication** — "Channel News Asia" and "亚洲新
  闻台" are separate logical channels even when they're the same
  broadcaster. Mixing them confuses the alive-pick logic.

## 3. Architecture overview

```
                          ┌─ refresh tick ─┐
                          │                │
              iptv-org    │                │
              free-tv     │  ingestion     │   logical_channels
              mjh-*       ├─► (existing)   ├──► (NEW table)
              fanmingming │                │   ◇ one row per
              yuechan     │                │     canonical channel
              openiptvitaly                │
              iptv-org-*  │                │
              youtube-live│                │
                          └──┬─────────────┘
                             │
                             ▼
                   dedup pipeline (NEW)
                   ◇ slug + country grouping
                   ◇ tvg-id family merge
                   ◇ curated overlay (YAML)
                             │
                             ▼
                   channel_sources (RENAMED from iptv_channels)
                   ◇ adds channel_id FK
                   ◇ otherwise unchanged

                   ▼   ▼   ▼   ▼   ▼   ▼
                   per-source rows for "CNA":
                     iptv-org:CNA.sg            → channel_id="cna"
                     iptv-org-sg:CNA.sg         → channel_id="cna"
                     free-tv:channel-news-asia  → channel_id="cna"
                     youtube-live:cna           → channel_id="cna"
```

Read path:

```
   /iptv (grid)              /api/iptv/channels        ──► returns logical_channels
                             /api/iptv/channels/<id>/sources ──► all sources for channel
   /iptv/play/<id>           /api/iptv/channels/<id>/play    ──► picks best alive,
                                                                  returns {url, source_id, alternates[]}
```

## 4. Schema

### 4.1. New table: `logical_channels`

```sql
CREATE TABLE IF NOT EXISTS logical_channels (
    id            TEXT PRIMARY KEY,        -- stable slug, e.g. "cna", "bbc-news"
    name          TEXT NOT NULL,            -- canonical display name
    country       TEXT,                     -- ISO 3166-1 alpha-2
    languages     TEXT,                     -- csv of ISO codes
    categories    TEXT,                     -- csv
    logo          TEXT,                     -- preferred logo URL
    aliases       TEXT,                     -- json array of also-known-as names
    is_curated    INTEGER NOT NULL DEFAULT 0,  -- 1 if from data/channel_aliases.yaml
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lc_country  ON logical_channels(country);
CREATE INDEX IF NOT EXISTS idx_lc_curated  ON logical_channels(is_curated);
```

### 4.2. Existing table extension: `iptv_channels` → semantically becomes `channel_sources`

We **add a column**, don't rename the table (avoids breaking the
existing /api/iptv/* endpoints during Phase 1).

```sql
ALTER TABLE iptv_channels ADD COLUMN channel_id TEXT;       -- FK → logical_channels.id
ALTER TABLE iptv_channels ADD COLUMN priority INTEGER NOT NULL DEFAULT 5;
CREATE INDEX IF NOT EXISTS idx_iptv_channel_id ON iptv_channels(channel_id);
```

`priority` is set per-source by the dedup pipeline (see §6). Higher
= more preferred. The `/play` endpoint picks the highest-priority
alive source.

### 4.3. Persistent view used by Phase 2 grid

```sql
CREATE VIEW IF NOT EXISTS v_channels_with_status AS
  SELECT
    lc.id, lc.name, lc.country, lc.categories, lc.logo,
    COUNT(cs.id) AS source_count,
    SUM(CASE WHEN cs.status = 'alive' THEN 1 ELSE 0 END) AS alive_count,
    MAX(CASE WHEN cs.status = 'alive' THEN cs.last_check_at END) AS last_alive_at
  FROM logical_channels lc
  LEFT JOIN iptv_channels cs ON cs.channel_id = lc.id
  GROUP BY lc.id;
```

Grid `/api/iptv/channels` does `SELECT … FROM v_channels_with_status
WHERE … ORDER BY name LIMIT N`. Counts per-channel source health in
the SQL pass — no app-level aggregation needed.

## 5. Deduplication algorithm

Runs at refresh time (existing `refresh_*` calls plus a new
`run_dedup()` step at the end of `refresh_all_sources`).

### 5.1. Normalisation

For each source row, compute a **bucket key**:

```python
def bucket_key(row) -> str | None:
    name = row.name
    # Strip noise suffixes / qualifiers iptv-org adds
    for tag in ("HD", "FHD", "1080p", "720p", "(1080p)", "(720p)",
                "[Geo-blocked]", "(Geo-blocked)", "[Backup]"):
        name = name.replace(tag, "")
    # Lowercase + collapse non-alnum to '-'
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        return None
    country = (row.country or "").upper() or "XX"
    return f"{slug}|{country}"
```

Channels with the same `bucket_key` are **candidate matches**.

### 5.2. tvg-id family merge

iptv-org sources use tvg-ids like `CNA.sg`, `CNA.sg@SD`, `CNA.sg@HD`.
These are the same channel. Rule:

```python
def tvg_family(tvg_id) -> str:
    # strip @SD/@HD/@1080p quality suffix
    return tvg_id.split("@", 1)[0]
```

Two rows with the same `tvg_family()` MUST be in the same logical
channel even if their names differ. tvg-id wins over slug bucketing
when they conflict.

### 5.3. Curated overrides

`data/channel_aliases.yaml` — human-curated mapping with absolute
authority:

```yaml
channels:
  cna:
    name: "Channel News Asia"
    country: SG
    aliases: ["CNA","CNA (1080p)","Channel News Asia HD"]
    sources:
      - "iptv-org:CNA.sg"           # exact source row id
      - "iptv-org:CNA.sg@SD"
      - "iptv-org-sg:CNA.sg"
      - "youtube-live:cna"
      - "free-tv:channel-news-asia"  # if it exists
    logo: "https://yt3.googleusercontent.com/.../=s176"
    priority_overrides:
      "youtube-live:cna": 10        # prefer YouTube for CNA
      "iptv-org:CNA.sg":  5
```

Curated channels:
- Skip the algorithmic bucketing (no false-positive risk).
- Set `is_curated=1` so Phase 2 can highlight them visually.
- Persist priority overrides into the row's `priority` column.

### 5.4. Pipeline order

```
run_dedup():
  1. Reset channel_id = NULL on all rows (idempotent fresh start)
  2. Apply curated overrides (data/channel_aliases.yaml)
     → upsert logical_channels (is_curated=1)
     → update channel_id on each named source row
  3. tvg-family pass over uncurated rows
     → group by tvg_family() within source='iptv-org*'
     → mint a logical_channel from the family head
  4. Slug+country pass over remaining uncurated rows
     → group by bucket_key()
     → mint logical_channel from group head (alphabetically first
       by name → deterministic across runs)
  5. Drop logical_channels with zero source rows (cleanup)
```

Expected outcome on current catalogue:
- ~50 curated channels (hand-picked top news)
- ~3000 tvg-family merged (iptv-org with quality variants)
- ~3000 slug+country merged (free-tv, mjh-*, openiptvitaly cross-overlap)
- Net: ~6000 logical channels from 11600 source rows (~50% reduction)

### 5.5. False positives

Slug+country bucketing can over-merge. Examples:
- "BBC News" (BBC's international) vs "BBC News HD" (UK-only)
  → same bucket. May be wrong if URL/feed differs.
- Two unrelated channels named "Sports HD" in same country
  → over-merged.

Mitigation:
- Curated overrides catch the high-traffic confusions.
- Phase 2 UI surfaces "merged from N sources" — easy to spot when
  the source dropdown has obviously-different channels.
- Owner can add an alias to split: `data/channel_aliases.yaml` can
  contain "DO NOT MERGE" markers (TBD design).

## 6. Source priority + failover

### 6.1. Default priority by source

```yaml
# Built-in source priorities (overridable per-channel in YAML).
# Higher = preferred when picking the alive source.

priorities:
  youtube-live:        10  # global CDN, geo-block-free, signed URLs
  iptv-org-sg:          9  # curated per-country, highest quality from iptv-org
  iptv-org-my:          9
  iptv-org-id:          9
  iptv-org:             7  # global JSON, comprehensive but less curated
  openiptvitaly:        6  # well-EPG'd, Italian only
  free-tv:              5  # community-curated, mixed quality
  mjh-sky-fast:         5  # NZ Sky FAST channels
  mjh-au:               4
  mjh-nz:               4
  fanmingming:          4
  yuechan:              4
  mjh-other:            3
  mjh-radio:            1  # mostly radio, lower play priority for TV
```

Persisted to a `data/source_priorities.yaml` (committed) so changes
don't need a code redeploy.

### 6.2. Best-alive pick

`/api/iptv/channels/<id>/play`:

```python
def pick_best_source(channel_id) -> dict:
    sources = list_sources(channel_id)        # all channel_sources rows
    sources.sort(key=lambda s: (
        s.priority,                            # higher first
        s.status == "alive",                   # alive over dead
        s.last_check_at or "1970",             # more-recent-checked first
    ), reverse=True)
    chosen = sources[0]
    return {
        "url":        chosen.url,
        "source_id":  chosen.id,
        "status":     chosen.status,
        "alternates": [
            {"id": s.id, "source": s.source, "status": s.status,
             "priority": s.priority, "url": s.url}
            for s in sources[1:]
        ],
    }
```

### 6.3. Client failover

Play page (existing inline-player JS):

```js
async function playWithFailover(channelId) {
  const { url, alternates } = await api(`/api/iptv/channels/${channelId}/play`);
  const queue = [{ url }, ...alternates];
  let i = 0;
  while (i < queue.length) {
    try {
      await playInlineSingle(queue[i].url);
      return;   // success
    } catch (e) {
      console.warn(`source ${queue[i].url} failed, trying next`);
      i++;
    }
  }
  toast('All sources failed.');
}
```

hls.js fires `Hls.Events.ERROR` on manifest-load or segment-fetch
failures. The handler catches, fails the promise, falls through to
the next alternate. dash.js has equivalent error events.

### 6.4. Re-pick on failure

When the client reports a source failure, server **demotes** that
source temporarily:

```
POST /api/iptv/sources/<source_row_id>/report_failure
  → marks status='dead', last_error='client-reported',
    bumps probe_count
```

Next `/play` call for the same channel skips it (alive-only sort).
Probe-all loop re-checks within 12h.

## 7. API surface

### 7.1. New endpoints (Phase 1)

```
GET  /api/iptv/channels                  ← logical channels list (replaces existing)
GET  /api/iptv/channels/<id>/sources     ← list all sources for a channel
GET  /api/iptv/channels/<id>/play        ← {url, source_id, alternates[]}
POST /api/iptv/sources/<source_id>/report_failure
POST /api/iptv/dedup/run                 ← manually re-run dedup pipeline (owner-only)
```

### 7.2. Backwards-compatible

```
GET  /iptv/play/<id>                      ← keep both:
                                             • logical-channel id (new)
                                             • source-prefixed id (old, e.g. "iptv-org:CNA.sg")
                                                redirect 302 → logical-channel URL
GET  /api/iptv/channels?source=…&country=…  ← existing filter params still work; now
                                              groups by channel_id, returns logical rows
```

### 7.3. Deprecated in Phase 3

```
GET  /iptv/play/iptv-org:CNA.sg           ← keeps working but emits Deprecation header
                                             pointing to /iptv/play/cna
```

## 8. UI changes

### 8.1. Grid (browse page)

- Card now represents a **logical channel**, not a source
- Bottom-right corner of card: small `×N` badge ("4 sources") if
  N > 1 — clickable expands a sources tooltip without leaving the grid
- Existing source/country/category filter chips work over
  `logical_channels` (filter resolves to "any channel with at least
  one source matching")
- Existing favorites list works at logical-channel level (preferred)

### 8.2. Play page

Top section adds a **source picker**:

```
Channel: Channel News Asia          🇸🇬 SG · news
[ ▾ source: YouTube Live (alive)  ◐ ]   ← dropdown
   • YouTube Live (alive, prio 10)        ← current
   • iptv-org-sg curated (alive, prio 9)
   • iptv-org global (alive, prio 7)
   • free-tv (dead, prio 5)
```

- Default selection = `pick_best_source()` result
- User can override; sticks for the play-page session (not persisted)
- `▶ Play` uses the currently-selected source
- Auto-failover: if hls.js errors, dropdown jumps to next alive
  alternate + toast notification

### 8.3. Sources tooltip on grid card

Tap the `×4` badge:

```
┌─ Sources ──────────────┐
│ ✅ YouTube Live  (10) │
│ ✅ iptv-org SG    (9) │
│ ✅ iptv-org       (7) │
│ ❌ free-tv         (5) │
└────────────────────────┘
```

Visual confirmation that the consolidation is correct. Owner-debug
mostly; users won't open it often.

## 9. Migration from current model

### 9.1. Order of operations

1. **Schema migrations run automatically** (idempotent ALTER + CREATE
   per init_iptv_schema convention). New tables empty.
2. **First refresh post-Phase-1** populates `logical_channels` via
   `run_dedup()`.
3. **Grid endpoint flips** to query `v_channels_with_status` view.
4. **Old URLs kept**: `/iptv/play/iptv-org:CNA.sg` is rewritten to
   `/iptv/play/cna` server-side via a 302. localStorage favorites
   need a one-shot migration (read old IDs → look up channel_id →
   re-save).

### 9.2. localStorage migration

```js
// Run once on first load of Phase-2 UI
async function _migrateFavorites() {
  const FAV_KEY_V1 = "smdl_iptv_favorites_v1";
  const FAV_KEY_V2 = "smdl_iptv_favorites_v2";
  if (localStorage.getItem(FAV_KEY_V2)) return;
  const oldIds = JSON.parse(localStorage.getItem(FAV_KEY_V1) || "[]");
  if (!oldIds.length) {
    localStorage.setItem(FAV_KEY_V2, "[]");
    return;
  }
  const r = await api("/api/iptv/sources/resolve_channels", {
    method: "POST", body: JSON.stringify({ source_ids: oldIds }),
  });
  // r.mapping = { "iptv-org:CNA.sg": "cna", ... }
  const newIds = [...new Set(Object.values(r.mapping))].filter(Boolean);
  localStorage.setItem(FAV_KEY_V2, JSON.stringify(newIds));
}
```

### 9.3. Auto-probe loop adjustments

- Sweeps remain at the source level (`channel_sources` rows). Logical
  channels inherit alive status from their best-source-alive.
- "Alive only" filter in the grid means "at least one source alive"
  for the logical channel.

## 10. Phases

| Phase | Effort | What ships |
|---|---|---|
| 0 | 1 hr | This doc |
| 1 | 1 day | Schema migrations, dedup pipeline, /play endpoint, source priorities YAML, /sources endpoint. **Old endpoints still work; new ones available.** Checkpoint review after. |
| 2 | half day | Grid + play page UI flip to logical channels. Source picker dropdown. Client failover. Favorites migration. Checkpoint review. |
| 3 | ongoing | Hand-curate `data/channel_aliases.yaml` for top channels. Hide noisy false-positives. Deprecate old `/iptv/play/<source>:<slug>` URLs. |

Total: ~2 days of focused work + checkpoint reviews + ongoing curation.

## 11. Open questions

1. **Channel ID stability across refreshes** — if iptv-org renames
   a channel, will its bucket_key change → new channel_id → favorites
   break? Mitigation: keep a `channel_id_history` table or rely on
   curated YAML for important channels.
2. **What about channels that exist in only one source?** — They
   still get a logical_channel row with one source. The grid card
   looks the same; the `×N` badge just isn't shown. No special-casing.
3. **Cross-country same-name channels** — "CNN" exists in US, IN, ID,
   etc. They're separate logical channels (different country) by
   design. The grid groups by country chip; that handles it.
4. **YouTube source URL caching** — youtube-live sources have
   per-play URL resolution (yt-dlp). The /play endpoint short-circuits
   for them: returns the @handle/live URL, frontend calls existing
   /resolve_url for the m3u8. Don't try to cache yt-dlp output in the
   /play response.
5. **DASH+HLS preference within a channel** — when a channel has both
   formats available, which wins? Decision: HLS preferred (broader
   client support — hls.js Just Works; dash.js is heavier). Priority
   tie-broken by format if same source provides both.
6. **Future Stremio addon export** — out of scope for v2, but the
   schema lines up if we ever want to publish the catalogue as a
   Stremio "channel directory" addon. logical_channels → catalogue
   items, channel_sources → addon streams. Free upgrade path.

## 12. Decision log

- **Logical channel id format**: lowercase kebab-case slugs. Same
  rules as scope names, makes them safe in URLs and YAML keys.
- **Where dedup runs**: at the end of `refresh_all_sources()` and as
  a manual `POST /api/iptv/dedup/run`. NOT per-row at insert time
  (too expensive to re-scan + re-merge for every channel).
- **Why a SQL view instead of a materialised table**: the grid query
  needs LEFT JOIN aggregates. A view recomputes on demand (~50 ms
  for 6000 rows). A materialised table would need triggers/refresh
  jobs and isn't worth the complexity at this scale.
- **Curated YAML lives in `metamcp-local/sentinel-vpn-dashboard/data/`**
  alongside `scopes.yaml`, not in the SMDL repo — channel curation
  is admin-side, not per-pillar.

---

**Sign-off**: this is the contract. Phase 1 implements against this
spec. Deviations require editing this doc first + a note in §13.

## 13. Changelog

- 2026-05-27 — initial draft (Phase 0). azfar.
