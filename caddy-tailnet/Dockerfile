# Custom Caddy with the Cloudflare DNS module, for ACME DNS-01 wildcard certs
# (*.svc.az-sentinel.xyz) on the tailnet HTTPS ecosystem.
# Mature parts only: Caddy core + caddy-dns/cloudflare. No experimental plugins.
# See sentinel-watchdog/planning/tailnet-https.md.
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
