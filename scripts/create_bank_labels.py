"""Create the Bank Statements label tree via google-workspace-mcp directly.
Uses the streamable HTTP MCP endpoint at :8089."""
import io, json, sys, urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

URL = "http://127.0.0.1:8089/mcp"

LABELS = [
    "Bank Statements",
    "Bank Statements/HSBC Statement",
    "Bank Statements/DBS Statement",
    "Bank Statements/Maybank Statement",
    "Bank Statements/SC Statement",
    "Bank Statements/UOB Statement",
]


def mcp_call(session_id: str | None, method: str, params: dict, msg_id: int):
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": method,
        "params": params,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        sid = resp.headers.get("mcp-session-id")
        raw = resp.read().decode("utf-8")
        # streamable-http returns SSE; pull the data: line
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                return sid, payload
        return sid, json.loads(raw)


# 1) initialize
sid, init = mcp_call(None, "initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "label-bootstrap", "version": "1"},
}, 1)
print(f"Session: {sid}")
print(f"Server: {init.get('result', {}).get('serverInfo', {}).get('name')}")

# 2) notifications/initialized (required handshake)
import urllib.request as ureq
notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
req = ureq.Request(URL, data=notif, headers={
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "mcp-session-id": sid,
}, method="POST")
ureq.urlopen(req, timeout=10).read()

# 3) call gmail_create_label for each
for i, name in enumerate(LABELS, start=2):
    _, resp = mcp_call(sid, "tools/call", {
        "name": "gmail_create_label",
        "arguments": {"name": name},
    }, i)
    result = resp.get("result", {}).get("content", [{}])[0].get("text", "")
    try:
        parsed = json.loads(result)
    except Exception:
        parsed = {"raw": result}
    flag = "✓ created" if parsed.get("created") else "= existing"
    print(f"  {flag}  {name}  →  id={parsed.get('id')}")

print("\n=== Final label list (Bank Statements only) ===")
_, resp = mcp_call(sid, "tools/call", {
    "name": "gmail_list_labels",
    "arguments": {},
}, 100)
labels = json.loads(resp["result"]["content"][0]["text"])
for l in sorted(labels, key=lambda x: x["name"]):
    if "Bank Statements" in l["name"]:
        print(f"  {l['type']:6}  {l['id']:20}  {l['name']}")
