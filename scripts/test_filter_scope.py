"""Probe whether filter API works under current token (will fail with 403 if scope missing)."""
import io, json, sys, urllib.request as r
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
URL = "http://127.0.0.1:8089/mcp"
def call(sid, method, params, mid):
    h = {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    if sid: h["mcp-session-id"] = sid
    body = json.dumps({"jsonrpc":"2.0","id":mid,"method":method,"params":params}).encode()
    with r.urlopen(r.Request(URL, data=body, headers=h, method="POST"), timeout=10) as resp:
        sid2 = resp.headers.get("mcp-session-id")
        raw = resp.read().decode()
        for ln in raw.splitlines():
            if ln.startswith("data:"):
                return sid2, json.loads(ln[5:])
        return sid2, json.loads(raw)
sid, _ = call(None, "initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}, 1)
r.urlopen(r.Request(URL, data=json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"}).encode(), headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream","mcp-session-id":sid}, method="POST"), timeout=5).read()
_, resp = call(sid, "tools/call", {"name":"gmail_list_filters","arguments":{}}, 2)
content = resp.get("result", {}).get("content", [])
text = content[0].get("text", "") if content else json.dumps(resp)
print("Response:", text[:400])
if "insufficient" in text.lower() or "403" in text or "scope" in text.lower():
    print("\n>> TOKEN MISSING SCOPE — re-auth required at http://localhost:8089/oauth")
elif "error" in text.lower() and "[" not in text[:5]:
    print("\n>> Other error — see above")
else:
    print("\n>> ✓ Scope OK, filters API reachable")
