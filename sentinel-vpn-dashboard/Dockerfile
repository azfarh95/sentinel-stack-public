FROM python:3.12-slim

# docker CLI is required so the service can `docker exec headscale` /
# `docker exec amneziawg` to read live VPN state. Pull just the CLI (not
# the daemon) — the docker socket is bind-mounted from the host.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py auth_v2.py users_db.py ./

# /data is bind-mounted from the host (sentinel-vpn-dashboard/data/) and
# carries the runtime SQLite (auth.db) + the read-mostly scope catalogue
# (scopes.yaml). Compose-managed; the dir is created if absent.
VOLUME /data

EXPOSE 8097
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8097", "--log-level", "info"]
