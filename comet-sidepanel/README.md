# comet-sidepanel

A Comet (or any Chromium 114+) side-panel extension that gives you a chat
window backed by your local OpenClaw / Qwen agent. The agent can act on the
active Comet tab via the existing Playwright MCP (CDP-attach on `:9222`),
plus everything else in OpenClaw's MetaMCP toolset.

```
┌─ Comet ──────────────────────────────────────────────┐
│                                                       │
│  ┌─ Active tab ────┐  ┌─ Side panel (this extension) ┐│
│  │                  │  │ User:  summarise this page  ││
│  │  webpage         │  │ Agent: (calls Playwright    ││
│  │                  │  │         snapshot, reads,    ││
│  │ ← actions via    │  │         summarises)         ││
│  │   Playwright CDP │  │ [type message ...........]  ││
│  └──────────────────┘  └─────────────────────────────┘│
└────────┬─────────────────────────┬────────────────────┘
         │ CDP :9222               │ HTTP :8101
         ▼                         ▼
   Playwright MCP            bridge.py (this folder)
   (existing :8931)                │
         ▲                         ▼ wsl openclaw agent --json
         │                  OpenClaw gateway (WSL2 :18789)
         └────  MetaMCP  ─────────┘
```

## One-time install

1. **Pick up the bridge** — run it once foreground to confirm `:8101` answers
   `/health` from Windows. Background-launched copy will start with the
   regular AI stack once integrated (see `bridge.py` header).
   ```powershell
   pythonw.exe C:\Users\azfar\metamcp-local\comet-sidepanel\bridge.py
   curl http://127.0.0.1:8101/health
   ```

2. **Load the unpacked extension** in Comet:
   - Visit `chrome://extensions` (works in Comet too).
   - Enable *Developer mode* (top-right toggle).
   - Click *Load unpacked* → select
     `C:\Users\azfar\metamcp-local\comet-sidepanel\extension`.
   - Pin the new "OpenClaw Sidepanel" icon.

3. **Launch Comet with CDP** when you want OpenClaw to drive the browser:
   ```powershell
   .\Launch-Comet-CDP.ps1
   ```
   (Or `Launch-Comet-CDP.bat` for double-click.) This closes any running
   Comet and relaunches with `--remote-debugging-port=9222`. Your session
   cookies/logins persist via Comet's regular user-data-dir.

4. **Verify the chain** with the doctor:
   ```powershell
   .\Comet-CDP-Doctor.ps1
   ```
   Every line should be `OK`. The most common failure is "Comet has
   --remote-debug flag" failing because Comet was launched the normal way.

5. **Open the side panel** by clicking the OpenClaw icon in the toolbar.
   First message has a ~70s cold start (Qwen3.6 + full bootstrap context);
   subsequent turns are ~5-15s.

## Day-to-day

- Talk in the side panel. Each Comet window gets its own session
  (`browser-win-<id>`), so two windows = two threads.
- The agent has access to the entire MetaMCP toolset: Playwright, Tavily
  search, Sentinel Finance, Gmail, Shopping MCP, etc. Anything OpenClaw can
  do from Telegram, it can do here.
- Page actions (click/type/screenshot) hit the **currently active** Comet
  tab through Playwright's `--shared-browser-context`.

## Files

| file                  | role                                                 |
| --------------------- | ---------------------------------------------------- |
| `bridge.py`           | HTTP shim on `:8101` → `wsl openclaw agent --json`   |
| `extension/`          | Chromium MV3 extension (sidePanel API)               |
| `Launch-Comet-CDP.*`  | Wrapper that launches Comet with `--remote-debugging-port=9222` |
| `Comet-CDP-Doctor.ps1`| Single-pass diagnostic of the whole chain            |

## Ports

| port  | who                            |
| ----- | ------------------------------ |
| 8101  | this bridge (was 8093 — moved because 8090–8100 is Hyper-V reserved) |
| 8931  | Playwright MCP (existing)      |
| 8932  | Playwright IPv4 proxy (existing) |
| 9222  | Comet CDP (only up when launched via the wrapper) |
| 12008 | MetaMCP                        |
| 18789 | OpenClaw gateway (WSL2)        |
