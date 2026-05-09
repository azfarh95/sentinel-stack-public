# MetaMCP Local Setup

A production-ready local MCP aggregation stack for Windows + Docker Desktop + WSL2, designed to serve multiple clients (LM Studio, OpenClaw, Cursor, Claude Desktop, etc.) through a unified MCP endpoint.

## Overview

**MetaMCP** is an MCP server that aggregates, orchestrates, and manages multiple MCP servers through a single unified endpoint. This setup allows you to:

- 🎯 **Aggregate MCP servers** into logical namespaces
- 🔌 **Expose endpoints** via SSE, Streamable HTTP, or OpenAPI
- 🔐 **Control access** with API key authentication or OAuth
- 🛠️ **Add/remove tools dynamically** without restarting clients
- 📊 **Manage multiple clients** from one control plane (this MetaMCP instance)

## Architecture

```
LM Studio ──┐
OpenClaw ──┼─→ MetaMCP (localhost:12008) ──→ [ MCP Servers ]
Cursor ────┤                                   - Google Drive
            └─ Connect to endpoint               - OneDrive
              via SSE or HTTP                    - Filesystem
                                                 - (Other MCPs added later)
```

## Services

### 1. **MetaMCP App** (`metamcp` container)
- **Image**: `ghcr.io/metatool-ai/metamcp:latest`
- **Port**: `12008` (web UI + SSE/HTTP endpoints)
- **Purpose**: MCP aggregation server, configuration UI, endpoint management
- **Tech Stack**: Next.js frontend, Express.js backend, tRPC APIs

### 2. **PostgreSQL** (`metamcp-pg` container)
- **Image**: `postgres:16-alpine`
- **Port**: `9433` (mapped from container 5432, internal only)
- **Purpose**: Persistent storage for MetaMCP configuration, users, API keys, namespaces, endpoints
- **Volume**: `metamcp_local_postgres_data` (Docker-managed, survives restarts)

## Quick Start

### Prerequisites
- Docker Desktop installed and running on Windows
- WSL2 backend enabled (Docker Desktop default on Windows)
- No existing services on ports `12008` (MetaMCP) or `9433` (PostgreSQL)

### Step 1: Start the Stack

```bash
cd ~/metamcp-local
docker compose -f docker-compose.local.yml up -d
```

**What happens:**
1. PostgreSQL container starts and initializes the database
2. MetaMCP waits for PostgreSQL to be healthy (~30 seconds)
3. MetaMCP starts, runs bootstrap configuration (creates default user, API keys, endpoints)
4. Both services are ready for connections

### Step 2: Access the Web UI

**URL**: `http://localhost:12008`

**Login credentials** (from `.env.local`):
- **Email**: `admin@localhost`
- **Password**: `changeme123` ⚠️ **Change this immediately in production**

### Step 3: Verify Services are Running

```bash
# Check container status
docker compose -f docker-compose.local.yml ps

# View logs
docker compose -f docker-compose.local.yml logs -f metamcp

# Check database health
docker compose -f docker-compose.local.yml logs -f postgres
```

### Step 4: Stop the Stack

```bash
cd ~/metamcp-local
docker compose -f docker-compose.local.yml down

# To also remove PostgreSQL data (⚠️ destructive):
docker compose -f docker-compose.local.yml down -v
```

## Configuration

### `.env.local` - Environment Variables

Key settings:

| Variable | Current Value | Purpose |
|----------|---------------|---------|
| `APP_URL` | `http://localhost:12008` | Public URL where MetaMCP is accessible. CORS strictly enforces this URL. |
| `POSTGRES_HOST` | `postgres` | Docker network hostname for PostgreSQL |
| `POSTGRES_PASSWORD` | `m3t4mcp` | PostgreSQL password (safe for local dev, change in production) |
| `BETTER_AUTH_SECRET` | `874any1sY8nCFX2aSxs49Iyl/5YLfDhcqgEMfYjpVCA=` | Session encryption key. **Keep secret.** |
| `LOG_LEVEL` | `all` | Logging verbosity (`all`, `info`, `errors-only`, `none`) |
| `BOOTSTRAP_USER_EMAIL` | `admin@localhost` | Default user email created on startup |
| `BOOTSTRAP_USER_PASSWORD` | `changeme123` | Default user password created on startup |

### Bootstrap Configuration (Auto-Create on Startup)

The following are created automatically on first run:

**API Keys:**
- `LM Studio` (private key for your LM Studio connection)
- `OpenClaw` (private key for your OpenClaw connection)

**Namespaces:**
1. `Default` - Public namespace for general tools
2. `Cloud Tools` - Namespace for Google Drive, OneDrive, and cloud integrations

**Endpoints:**
1. `default` - Public endpoint (no auth required)
   - URL: `http://localhost:12008/metamcp/default/sse`
   - Purpose: Test endpoint, initial tool discovery
2. `cloud` - Private endpoint (API key required)
   - URL: `http://localhost:12008/metamcp/cloud/sse`
   - Purpose: Cloud storage tools with authentication

## Connecting Clients to MetaMCP

### General Endpoint URL Format

```
http://localhost:12008/metamcp/{ENDPOINT_NAME}/{TRANSPORT}
```

**Supported Transports:**
- `sse` - Server-Sent Events (recommended for most clients)
- `mcp` - Streamable HTTP (for Claude Desktop via `mcp-proxy`)
- `openapi` - OpenAPI (for REST-based tools and Open WebUI)

### LM Studio Integration

**Step 1:** Retrieve API keys from MetaMCP web UI

1. Navigate to `http://localhost:12008`
2. Login with `admin@localhost` / `changeme123`
3. Go to **Settings** → **API Keys**
4. Copy the "LM Studio" API key (format: `sk_mt_...`)

**Step 2:** Configure LM Studio

In LM Studio settings (MCP section):

```json
{
  "mcpServers": {
    "MetaMCP": {
      "url": "http://localhost:12008/metamcp/default/sse",
      "headers": {
        "Authorization": "Bearer sk_mt_YOUR_LM_STUDIO_KEY_HERE"
      }
    }
  }
}
```

**Note:** 
- Use the public `default` endpoint for initial testing
- Switch to `cloud` endpoint after adding Google Drive / OneDrive MCPs
- Replace `sk_mt_YOUR_LM_STUDIO_KEY_HERE` with your actual API key

### OpenClaw Integration

**Step 1:** Get the API key from MetaMCP (same as LM Studio)

**Step 2:** Configure OpenClaw

Add to OpenClaw configuration:

```json
{
  "mcpServers": {
    "MetaMCP": {
      "url": "http://localhost:12008/metamcp/default/sse",
      "headers": {
        "Authorization": "Bearer sk_mt_YOUR_OPENCLAW_KEY_HERE"
      }
    }
  }
}
```

### Cursor Integration

Cursor supports SSE directly without a proxy:

```json
{
  "mcpServers": {
    "MetaMCP": {
      "url": "http://localhost:12008/metamcp/default/sse"
    }
  }
}
```

### Claude Desktop (STDIO-only)

Claude Desktop requires a local proxy because it only supports STDIO. Use `mcp-proxy`:

```json
{
  "mcpServers": {
    "MetaMCP": {
      "command": "uvx",
      "args": [
        "mcp-proxy",
        "http://localhost:12008/metamcp/default/sse"
      ],
      "env": {
        "API_ACCESS_TOKEN": "sk_mt_YOUR_CLAUDE_DESKTOP_KEY_HERE"
      }
    }
  }
}
```

## Logs and Debugging

### View Application Logs

```bash
# Real-time logs (MetaMCP app)
docker compose -f docker-compose.local.yml logs -f metamcp

# PostgreSQL logs
docker compose -f docker-compose.local.yml logs -f postgres

# All services
docker compose -f docker-compose.local.yml logs -f
```

### Container Logs Location

Inside the MetaMCP container:
- `app.log` - Application, debug, info, and warnings
- `error.log` - Errors only

### Change Log Level

Edit `.env.local`:
```bash
LOG_LEVEL=all  # options: all, info, errors-only, none
```

Then restart:
```bash
docker compose -f docker-compose.local.yml restart metamcp
```

## Database

### PostgreSQL Details

| Property | Value |
|----------|-------|
| Host | `postgres` (Docker network) |
| Port | `5432` (internal) / `9433` (host) |
| Database | `metamcp_db` |
| User | `metamcp_user` |
| Password | `m3t4mcp` (in `.env.local`) |

### Connect Directly (Optional)

If needed, connect via psql:

```bash
docker exec -it metamcp-pg psql -U metamcp_user -d metamcp_db -h localhost
```

### Backup & Restore

**Backup:**
```bash
docker compose -f docker-compose.local.yml exec postgres pg_dump -U metamcp_user metamcp_db > backup.sql
```

**Restore:**
```bash
docker compose -f docker-compose.local.yml exec -T postgres psql -U metamcp_user metamcp_db < backup.sql
```

## MCP Servers (Next Phase)

Currently, the setup is ready to aggregate MCP servers. The following are planned for Phase 2:

### 1. **Filesystem MCP** (Built-in / Local)
   - Access local files and folders
   - Read, write, search operations
   - Part of official MCP servers

### 2. **Google Drive MCP**
   - Access Google Drive files
   - Search, list, read files
   - Part of official MCP servers

### 3. **OneDrive MCP**
   - Access Microsoft OneDrive/SharePoint
   - List files, search, download
   - Part of official MCP servers

### 4. **Browser Automation MCP** (Playwright-based)
   - Automate browser tasks
   - Take screenshots, fill forms, click buttons
   - Community-maintained

### 5. **Shared Memory MCP** (Optional)
   - Persistent memory across sessions
   - User-specific or global memory
   - Useful for agent continuity

### 6. **yt-dlp MCP** (Optional)
   - Download YouTube videos and playlists
   - Extract metadata
   - Community-maintained

### 7. **Google Maps MCP** (Optional)
   - Search locations, directions, place details
   - Requires Google Maps API key
   - Community-maintained

---

## Troubleshooting

### Port Already in Use

```bash
# Check what's using port 12008
netstat -ano | findstr :12008

# Kill the process (if needed)
taskkill /PID <PID> /F
```

### PostgreSQL Connection Error

```bash
# Check if postgres container is healthy
docker compose -f docker-compose.local.yml ps

# Restart postgres
docker compose -f docker-compose.local.yml restart postgres

# Check logs
docker compose -f docker-compose.local.yml logs postgres
```

### Can't Access http://localhost:12008

1. Verify containers are running: `docker compose -f docker-compose.local.yml ps`
2. Check logs: `docker compose -f docker-compose.local.yml logs metamcp`
3. Verify app started: look for "Server running on port 12008" in logs
4. Try from WSL: `curl http://localhost:12008`

### CORS Issues

MetaMCP strictly enforces `APP_URL` for CORS. Make sure:
- You're accessing via `http://localhost:12008` (not `127.0.0.1` or IP address)
- `APP_URL` in `.env.local` matches your access URL
- If behind a proxy, update `APP_URL` to the proxy URL

### Bootstrap Configuration Not Applied

- Check `.env.local` syntax (JSON arrays must be valid)
- Set `BOOTSTRAP_DEBUG=true` to see detailed bootstrap logs
- Set `BOOTSTRAP_FAIL_HARD=false` (default) to allow app to start even if bootstrap fails
- Restart: `docker compose -f docker-compose.local.yml restart metamcp`

---

## Backup & Restore

### Backup Everything

```bash
# Create backup folder with timestamp
mkdir -p ~/metamcp-backups/backup-$(date +%Y%m%d-%H%M%S)

# Backup .env.local
cp .env.local ~/metamcp-backups/backup-$(date +%Y%m%d-%H%M%S)/

# Backup PostgreSQL
docker compose -f docker-compose.local.yml exec postgres pg_dump -U metamcp_user metamcp_db > ~/metamcp-backups/backup-$(date +%Y%m%d-%H%M%S)/metamcp_db.sql

# Backup Docker volume (if needed for manual recovery)
docker run --rm -v metamcp_local_postgres_data:/data -v ~/metamcp-backups:/backup alpine tar czf /backup/backup-$(date +%Y%m%d-%H%M%S)/postgres_volume.tar.gz /data
```

### Restore (if catastrophic failure)

```bash
# Restore .env.local
cp ~/metamcp-backups/backup-YYYY-MM-DD-HH-MM-SS/.env.local .env.local

# Stop services
docker compose -f docker-compose.local.yml down

# Remove old volume
docker volume rm metamcp_local_postgres_data

# Restart (PostgreSQL will be empty)
docker compose -f docker-compose.local.yml up -d postgres

# Wait for postgres to be ready
sleep 10

# Restore database from SQL
docker compose -f docker-compose.local.yml exec -T postgres psql -U metamcp_user metamcp_db < ~/metamcp-backups/backup-YYYY-MM-DD-HH-MM-SS/metamcp_db.sql

# Start app
docker compose -f docker-compose.local.yml up -d app
```

---

## Integration Checklist

### Phase 1: Verify MetaMCP is Running ✓

- [ ] MetaMCP web UI loads at `http://localhost:12008`
- [ ] Can login with `admin@localhost` / `changeme123`
- [ ] Default endpoint is accessible

### Phase 2: Integrate LM Studio

- [ ] Retrieve "LM Studio" API key from MetaMCP Settings → API Keys
- [ ] Add MetaMCP to LM Studio MCP configuration
- [ ] Test with `default` endpoint first
- [ ] Verify tools appear in LM Studio

### Phase 3: Integrate OpenClaw

- [ ] Retrieve "OpenClaw" API key from MetaMCP Settings → API Keys
- [ ] Add MetaMCP to OpenClaw configuration
- [ ] Test connection
- [ ] Verify tools appear in OpenClaw

### Phase 4: Add Filesystem MCP (Coming)

- [ ] Deploy Filesystem MCP in MetaMCP
- [ ] Add to "Default" namespace
- [ ] Test file access from LM Studio

### Phase 5: Add Google Drive MCP (Coming)

- [ ] Set up Google Drive API credentials
- [ ] Deploy Google Drive MCP in MetaMCP
- [ ] Add to "Cloud Tools" namespace
- [ ] Test file access from LM Studio and OpenClaw

### Phase 6: Add OneDrive MCP (Coming)

- [ ] Set up Microsoft Graph API credentials
- [ ] Deploy OneDrive MCP in MetaMCP
- [ ] Add to "Cloud Tools" namespace
- [ ] Test file access from LM Studio and OpenClaw

---

## File Structure

```
~/metamcp-local/
├── docker-compose.local.yml    # Local Docker Compose config
├── .env.local                   # Environment variables (DO NOT commit secrets)
├── docker-compose.yml           # Original official config (reference)
├── example.env                  # Original example env (reference)
├── Dockerfile                   # MetaMCP Dockerfile (for custom builds)
├── apps/                        # MetaMCP source (frontend/backend)
├── packages/                    # MetaMCP shared packages
├── docs/                        # MetaMCP documentation
└── README.md                    # This file
```

---

## Next Steps

### Immediately

1. ✅ Start MetaMCP: `docker compose -f docker-compose.local.yml up -d`
2. ✅ Access web UI: `http://localhost:12008`
3. ✅ Change default password: Settings → Users → Edit admin@localhost
4. ✅ Copy API keys for LM Studio and OpenClaw

### Short-term (This Week)

1. Integrate LM Studio (Phase 2 above)
2. Integrate OpenClaw (Phase 3 above)
3. Plan MCP server additions

### Medium-term (This Month)

1. Add Filesystem MCP (read/write local files)
2. Add Google Drive MCP (cloud storage access)
3. Add OneDrive MCP (enterprise cloud storage)
4. Document MCP server configs in MetaMCP

### Production Deployment (Future)

- Migrate to a VPS or dedicated server (2GB-4GB RAM recommended)
- Set up HTTPS with Let's Encrypt
- Configure OIDC/SSO for enterprise auth
- Set up automated backups
- Configure Nginx reverse proxy with SSE support (see `nginx.conf.example`)

---

## Resources

- **MetaMCP Documentation**: https://docs.metamcp.com
- **MetaMCP GitHub**: https://github.com/metatool-ai/metamcp
- **MCP Specification**: https://spec.modelcontextprotocol.io/
- **Official MCP Servers**: https://github.com/modelcontextprotocol/servers
- **Docker Desktop**: https://www.docker.com/products/docker-desktop

---

## Support & Issues

- **MetaMCP Issues**: https://github.com/metatool-ai/metamcp/issues
- **Discord**: https://discord.gg/mNsyat7mFX
- **Documentation**: https://docs.metamcp.com

---

**Last Updated**: 2026-04-28  
**Setup Version**: 1.0.0  
**MetaMCP Version**: Latest (auto-pulled from GHCR)
