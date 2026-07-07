# OpenClaw SHADOW image — a containerized CLONE for clone-first validation
# (see ../planning/openclaw-containerization-plan.md). Same version + registry as
# the live WSL install (openclaw@2026.6.1) so it's a faithful clone, not a guess.
# This image is NOT wired into the live Dove path — it runs as an isolated shadow
# gateway on its own port + its own ~/.openclaw volume until Phase 4 cutover.
FROM node:22-bookworm-slim

# OpenClaw + some skills shell out (git/curl) and a few of its 55 deps may build
# native bits; include a toolchain so `npm i -g` can't fail on a native module.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

# Faithful clone: same version as the live WSL install (engines: node >=22.19.0).
RUN npm install -g openclaw@2026.6.1 && npm cache clean --force

ENV HOME=/root
ENV OPENCLAW_GATEWAY_PORT=18789
EXPOSE 18789

# Default = the gateway server; per-turn agent runs via `docker exec`.
ENTRYPOINT ["openclaw"]
CMD ["gateway", "--port", "18789"]
