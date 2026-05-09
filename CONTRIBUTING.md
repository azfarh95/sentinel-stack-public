# Contributing to Sentinel Stack

Sentinel Stack is a personal AI assistant infrastructure project. Contributions are welcome — whether that's bug reports, new MCP server integrations, Mini App improvements, or documentation fixes.

---

## Project Structure

```
sentinel-stack/
├── sentinel-miniapp-v2/     # Telegram Mini App dashboard (Flask bridge + vanilla JS)
├── watchdog/                # Watchdog bot (Windows Task Scheduler, Python)
├── google-workspace-mcp/    # Google Calendar, Drive, Gmail MCP server
├── maps-mcp/                # Google Maps directions + search MCP server
├── memory-mcp/              # Long-term memory MCP server (SQLite-vec)
├── reminders-mcp/           # APScheduler-based Telegram reminders MCP server
├── onedrive-mcp/            # OneDrive + Azure Document Intelligence MCP server
├── translate-mcp/           # LibreTranslate wrapper MCP server
├── ytdlp-mcp/               # yt-dlp + gallery-dl video/photo download MCP server
├── scripts/                 # Stack management scripts (start/stop/backup/bump)
├── docs/                    # Documentation (miniapp, watchdog, config, llm-prompt)
├── workspace/               # OpenClaw system prompt (SOUL.md, TOOLS.md)
└── docker-compose.local.yml # Full stack compose file
```

---

## Getting Started

### Prerequisites

- Windows 11 with WSL2 (Ubuntu-24.04)
- Docker Desktop
- Python 3.11+ (`py` launcher)
- Node.js + npm (for OpenClaw inside WSL2)
- LM Studio (optional — for local model inference)

### Setup

1. Clone the repo:
   ```powershell
   git clone https://github.com/azfarh95/sentinel-stack-public.git metamcp-local
   cd metamcp-local
   ```

2. Copy config templates:
   ```powershell
   copy config.example.json config.json
   copy watchdog\config.example.json watchdog\config.json
   ```

3. Create `.env.local` from the documented variables in `docker-compose.local.yml`.

4. Start the stack:
   ```powershell
   scripts\START_AI_STACK.bat
   ```

See [docs/llm-prompt.md](docs/llm-prompt.md) for the full step-by-step installation guide (written so an LLM can execute it for you).

---

## Adding a New MCP Server

Each MCP server lives in its own directory with a `Dockerfile`, `requirements.txt`, and a FastMCP `app/main.py`. To add one:

1. Create `your-mcp/` following the structure of an existing server (e.g. `maps-mcp/`).
2. Add the service to `docker-compose.local.yml` with a `127.0.0.1`-bound port.
3. Register it in MetaMCP via the web UI at `http://localhost:12008`.
4. Add the tool routing rule to `workspace/TOOLS.md` and `workspace/SOUL.md`.
5. Update the `SERVICES` list in `sentinel-miniapp-v2/bridge.py` so the Mini App monitors it.

---

## Development Tips

- **MCP servers**: use FastMCP. Guard stateful singletons with `if not x.running` in the lifespan context (see `reference_fastmcp_lifespan` in project memory).
- **Bridge**: `sentinel-miniapp-v2/bridge.py` is a plain Flask app. Restart it after any change: kill the process on `:8098` and relaunch with `py -3 bridge.py`.
- **Watchdog**: edit `watchdog/watchdog.py`, then stop and restart the Task Scheduler task (`Sentinel Watchdog`).
- **OpenClaw prompts**: edit `workspace/SOUL.md` (Windows tracking copy) and sync the change to `\\wsl.localhost\Ubuntu-24.04\home\<user>\.openclaw\workspace\SOUL.md`.

---

## Reporting Issues

Use [GitHub Issues](https://github.com/azfarh95/sentinel-stack-public/issues) to report bugs or suggest improvements.

When reporting a bug, include:
- Which component is affected (watchdog, a specific MCP server, Mini App, etc.)
- Steps to reproduce
- Relevant log output (`docker logs <container>`, `journalctl -u openclaw-gateway.service`, etc.)

---

## Pull Requests

- Keep changes focused — one concern per PR.
- If you're modifying a config file, make sure secrets stay out (use `.env.local` or `config.json`, both gitignored).
- Update `README.md` if you add a new service or change a port.

---

## License

All Rights Reserved. See `LICENSE`.
