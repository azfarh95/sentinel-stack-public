# sentinel-shopping-mcp — Docker container for MetaMCP-native deployment.
#
# Includes Chromium + Xvfb so nodriver can run headful inside the container.
# Headful is important: the whole anti-detection story (Akamai, Cloudflare Bot
# Manager) hinges on the browser not looking automated. Headless nodriver still
# beats some shields but loses the edge.
#
# Compose entry pairs this with metamcp-network so MetaMCP reaches it at
# http://shopping-mcp:8100/mcp.

FROM python:3.12-slim-bookworm

# Install Chromium + Xvfb + Chrome dependencies in a single layer to keep
# the image lean. dbus-x11 silences a noisy warning on startup. tini supervises
# the main process so signals propagate cleanly.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    dbus-x11 \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    libgbm1 \
    libgtk-3-0 \
    libdrm2 \
    ca-certificates \
    tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir 'beautifulsoup4>=4.12' 'lxml>=5.0' 'uvicorn[standard]>=0.30'

# Copy source. Order: change-rarely first, then app code.
COPY schema.py storage.py mcp_server.py ./
COPY adapters ./adapters

# nodriver looks for Chromium in PATH. /usr/bin/chromium is the Debian binary.
ENV NODRIVER_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    CHROME_BIN=/usr/bin/chromium \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1

EXPOSE 8100

# Xvfb provides a virtual display so headful Chrome doesn't need a real screen.
# We start it in the background, then run uvicorn against the FastMCP app.
# The `& sleep 1` lets Xvfb bind before Chrome tries.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 -ac +extension RANDR & sleep 1 && exec uvicorn mcp_server:app --host 0.0.0.0 --port 8100 --log-level info"]
