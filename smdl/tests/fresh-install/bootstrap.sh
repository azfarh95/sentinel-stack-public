#!/usr/bin/env bash
# Run inside the freshly-imported smdl-test WSL distro.
# Installs the prerequisites a stranger would need: docker, git, ffmpeg.
#
# Exits non-zero on any failure so the orchestrator (run-wsl2-test.ps1) can
# tell the test failed.

set -euo pipefail

echo "── apt update ──────────────────────────────────────────────"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

echo "── install: docker.io, docker-compose-v2, git, ffmpeg, curl, ca-certificates ──"
apt-get install -y -qq \
    docker.io \
    docker-compose-v2 \
    git \
    ffmpeg \
    curl \
    ca-certificates

echo "── enable + start dockerd ──────────────────────────────────"
# WSL2 doesn't run systemd by default unless wsl.conf says so. Start dockerd
# in a backgrounded screen to keep it alive for the test duration.
if ! pgrep -f dockerd > /dev/null; then
    nohup dockerd > /var/log/dockerd.log 2>&1 &
    sleep 4
fi
docker info > /dev/null || (echo "ERROR: dockerd not responding"; cat /var/log/dockerd.log; exit 1)
echo "  dockerd up"

echo "── verify versions ─────────────────────────────────────────"
docker --version
docker compose version
git --version
ffmpeg -version | head -1

echo "── bootstrap done ──────────────────────────────────────────"
