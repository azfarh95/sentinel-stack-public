# Sentinel Box Link

The phone's authenticated **tailnet** channel to the box — the shared spine for
Sentinel AI Mobile's Phase C (MCP tools + self-update), D (Dove), and E (hive).

Reached by the phone at `https://boxlink.svc.your-domain.example.com` via caddy-tailnet
(`reverse_proxy host.docker.internal:8130`). **Tailnet-only**, so it is NOT behind
Cloudflare Access — which is why the in-app self-update version check rides this
instead of the CF-gated `/api/apps`.

## Endpoints

- `GET /health` — liveness.
- `GET /update/{app_id}?current=<ver>` — latest app version (from the apps-hub
  `manifest.json`) + the public APK URL + `update_available`. Drives in-app self-update.
- _(later)_ `/mcp/*` proxy to metamcp (Scout's tools); `/dove/turn` (Phase D);
  `/memory/*` (Phase E).

## Auth

Static bearer token `BOX_LINK_TOKEN` (owner-only; the phone carries the same token).
Unset = open (dev). Add to `metamcp-local/.env.local` + the watchdog secret manifest.

## Deploy

Docker service in `metamcp-local/docker-compose.yml` (publishes host `:8130`, mounts
`sentinel-apps` read-only at `/apps`). Add a caddy-tailnet route:

```
@boxlink host boxlink.svc.your-domain.example.com
handle @boxlink { reverse_proxy host.docker.internal:8130 }
```

then `docker exec caddy-tailnet caddy reload --config /etc/caddy/Caddyfile`.
