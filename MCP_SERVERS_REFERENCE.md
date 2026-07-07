# MCP Servers - Reference for Phase 2+ Integration

This document outlines the MCP servers recommended for your setup, their sources, and how to integrate them into MetaMCP.

## Official MCP Servers (Recommended)

These are maintained by Anthropic and the Model Context Protocol team.

### 1. Filesystem MCP
**Purpose**: Read, write, and manage local files and directories  
**Repository**: `modelcontextprotocol/servers/filesystem`  
**Command**: `npx -y @modelcontextprotocol/filesystem`  
**Documentation**: https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem

**MetaMCP Configuration**:
```json
{
  "FilesystemMCP": {
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/filesystem", "/home/user"]
  }
}
```

**Notes**:
- Replace `/home/user` with the directory you want to expose
- Consider security implications (read-only vs read-write)
- Can be restricted to specific folders

---

### 2. Google Drive MCP
**Purpose**: Access Google Drive files, search, read, and manage documents  
**Repository**: `modelcontextprotocol/servers/google-drive`  
**Command**: `npx -y @modelcontextprotocol/google-drive`  
**Documentation**: https://github.com/modelcontextprotocol/servers/tree/main/src/google-drive

**Setup Requirements**:
1. Create a Google Cloud project
2. Enable Google Drive API
3. Create a service account and download credentials JSON
4. Share a Google Drive folder with the service account email

**MetaMCP Configuration**:
```json
{
  "GoogleDriveMCP": {
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/google-drive"],
    "env": {
      "GOOGLE_DRIVE_CREDENTIALS_JSON": "${GOOGLE_DRIVE_CREDENTIALS}",
      "GOOGLE_DRIVE_FOLDER_ID": "${GOOGLE_DRIVE_FOLDER_ID}"
    }
  }
}
```

**Environment Variables** (add to `.env.local`):
```bash
# Base64-encoded Google Drive service account JSON
GOOGLE_DRIVE_CREDENTIALS=<base64_encoded_json_here>

# Google Drive folder ID to expose (get from Drive URL)
GOOGLE_DRIVE_FOLDER_ID=<folder_id_here>
```

---

### 3. OneDrive MCP
**Purpose**: Access Microsoft OneDrive and SharePoint files  
**Repository**: `modelcontextprotocol/servers/onedrive`  
**Command**: `npx -y @modelcontextprotocol/onedrive`  
**Documentation**: https://github.com/modelcontextprotocol/servers/tree/main/src/onedrive

**Setup Requirements**:
1. Create an Azure App Registration
2. Grant Microsoft Graph permissions (Files.Read, Files.ReadWrite, etc.)
3. Create client credentials or certificate
4. Get your OneDrive item ID (or use "root" for your main drive)

**MetaMCP Configuration**:
```json
{
  "OneDriveMCP": {
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/onedrive"],
    "env": {
      "AZURE_TENANT_ID": "${AZURE_TENANT_ID}",
      "AZURE_CLIENT_ID": "${AZURE_CLIENT_ID}",
      "AZURE_CLIENT_SECRET": "${AZURE_CLIENT_SECRET}",
      "ONEDRIVE_FOLDER_ID": "${ONEDRIVE_FOLDER_ID}"
    }
  }
}
```

**Environment Variables** (add to `.env.local`):
```bash
# Azure / Microsoft Graph credentials
AZURE_TENANT_ID=<your_tenant_id>
AZURE_CLIENT_ID=<your_client_id>
AZURE_CLIENT_SECRET=<your_client_secret>

# OneDrive folder/drive ID (or "root" for main drive)
ONEDRIVE_FOLDER_ID=root
```

---

### 4. Fetch MCP
**Purpose**: Make HTTP requests, fetch URLs, scrape web content  
**Repository**: `modelcontextprotocol/servers/fetch`  
**Command**: `npx -y @modelcontextprotocol/fetch`  
**Documentation**: https://github.com/modelcontextprotocol/servers/tree/main/src/fetch

**MetaMCP Configuration**:
```json
{
  "FetchMCP": {
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/fetch"]
  }
}
```

**Notes**:
- No special credentials required
- Useful for web scraping and data fetching
- Can be restricted to safe domains

---

## Community MCP Servers (Optional)

These are community-maintained and may require additional setup.

### Browser Automation MCP (Playwright-based)
**Purpose**: Automate browser tasks, take screenshots, fill forms  
**Repository**: `modelcontextprotocol/servers/browser` (or community alternatives)  
**Command**: `npx -y @modelcontextprotocol/browser`

**MetaMCP Configuration**:
```json
{
  "BrowserMCP": {
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/browser"]
  }
}
```

---

### yt-dlp MCP (Community)
**Purpose**: Download YouTube videos, playlists, extract metadata  
**Repository**: Community-maintained (search GitHub for "yt-dlp mcp")  
**Command**: `python -m mcp_yt_dlp` (or similar)

**Prerequisites**:
- Python 3.8+
- yt-dlp library

**MetaMCP Configuration**:
```json
{
  "YtDlpMCP": {
    "type": "STDIO",
    "command": "python",
    "args": ["-m", "mcp_yt_dlp"]
  }
}
```

---

### Google Maps MCP (Community)
**Purpose**: Search locations, directions, place details  
**Repository**: Community-maintained (search GitHub for "google-maps mcp")

**Setup Requirements**:
1. Google Cloud project with Maps API enabled
2. API key with appropriate permissions

**MetaMCP Configuration**:
```json
{
  "GoogleMapsMCP": {
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@your-org/google-maps-mcp"],
    "env": {
      "GOOGLE_MAPS_API_KEY": "${GOOGLE_MAPS_API_KEY}"
    }
  }
}
```

**Environment Variables** (add to `.env.local`):
```bash
GOOGLE_MAPS_API_KEY=<your_api_key>
```

---

### Shared Memory MCP (Community)
**Purpose**: Persistent memory across sessions for agents  
**Repository**: Community-maintained (search for "memory mcp" or "persistent-memory mcp")

**MetaMCP Configuration**:
Depends on the specific implementation; consult the repository.

---

## Adding MCP Servers to MetaMCP

### Via Web UI

1. Navigate to `http://localhost:12008/settings/mcp-servers`
2. Click "Add MCP Server"
3. Fill in:
   - **Name**: Display name (e.g., "Google Drive")
   - **Type**: `STDIO` (for command-based servers)
   - **Command**: `npx`, `python`, etc.
   - **Args**: Command arguments as a JSON array
   - **Environment Variables**: Any required env vars
4. Click "Save"
5. Add the server to a namespace in the Namespaces tab

### Via Configuration File (Bootstrap)

Edit `.env.local` and add MCP servers to the bootstrap configuration:

```json
BOOTSTRAP_MCP_SERVERS=[
  {
    "name": "Filesystem",
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/filesystem", "/home/user"]
  },
  {
    "name": "Google Drive",
    "type": "STDIO",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/google-drive"],
    "env": {
      "GOOGLE_DRIVE_CREDENTIALS_JSON": "${GOOGLE_DRIVE_CREDENTIALS}",
      "GOOGLE_DRIVE_FOLDER_ID": "${GOOGLE_DRIVE_FOLDER_ID}"
    }
  }
]
```

Then restart:
```bash
docker compose -f docker-compose.local.yml restart metamcp
```

---

## Testing an MCP Server

### From LM Studio

1. Add the MetaMCP endpoint to LM Studio
2. Open a chat and ask for available tools
3. Look for tools from the MCP server you added
4. Test a tool call

### Manual Testing (curl)

```bash
# Test the endpoint
curl -X POST http://localhost:12008/metamcp/default/mcp \
  -H "Content-Type: application/json" \
  -d '{"method":"tools/list"}'
```

---

## Troubleshooting MCP Server Integration

### Server Not Appearing in Tools List

1. Check MetaMCP logs: `docker compose -f docker-compose.local.yml logs -f metamcp`
2. Verify the MCP server command is correct
3. Ensure environment variables are properly set
4. Try testing the command manually in a container:
   ```bash
   docker compose -f docker-compose.local.yml exec metamcp npx -y @modelcontextprotocol/fetch
   ```

### Tool Calls Failing

1. Check logs for error details
2. Verify credentials are correct (for cloud services)
3. Test the underlying tool (e.g., test Google Drive access)
4. Set `LOG_LEVEL=all` in `.env.local` for detailed debugging

### Cold Start Times

- MetaMCP pre-allocates idle sessions to reduce cold start times
- If an MCP server is slow to start, it may be killed on first use
- Custom Dockerfile can pre-install dependencies

---

## Security Considerations

### Credentials Management

- Never commit `.env.local` to version control
- Use environment variable references: `${VAR_NAME}` instead of hardcoded values
- For base64-encoded credentials, keep the .env file local-only
- Use secrets management tools for production (HashiCorp Vault, AWS Secrets, etc.)

### Exposed Folders

- Filesystem MCP: Restrict to specific directories
- Only expose what's necessary
- Consider read-only access when possible

### API Keys

- Rotate API keys regularly
- Use separate keys for development vs. production
- Consider creating separate Google Drive / OneDrive folders for dev/prod

---

## Next Steps

1. **Verify MetaMCP is running** (Phase 1 in PROJECT_README.md)
2. **Integrate LM Studio** (Phase 2)
3. **Add Filesystem MCP** (simple starting point)
4. **Add Google Drive or OneDrive** (cloud storage)
5. **Add browser automation** (if needed)

For each addition, follow the "Via Web UI" steps above.

---

## Resources

- **MetaMCP MCP Server Configuration**: https://docs.metamcp.com/mcp-servers/configuration
- **Official MCP Servers**: https://github.com/modelcontextprotocol/servers
- **MCP Specification**: https://spec.modelcontextprotocol.io/
- **Google Drive API Setup**: https://developers.google.com/drive/api/guides/about-sdk
- **Microsoft Graph API**: https://learn.microsoft.com/en-us/graph/overview

---

**Version**: 1.0.0  
**Last Updated**: 2026-04-28
