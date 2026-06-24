"""Debug filter creation error."""
import io, json, sys, urllib.request as r
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
URL = "http://127.0.0.1:8089/mcp"
def call(sid, method, params, mid):
    h = {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    if sid: h["mcp-session-id"] = sid
    body = json.dumps({"jsonrpc":"2.0","id":mid,"method":method,"params":params}).encode()
    with r.urlopen(r.Request(URL, data=body, headers=h, method="POST"), timeout=15) as resp:
        sid2 = resp.headers.get("mcp-session-id")
        raw = resp.read().decode()
        for ln in raw.splitlines():
            if ln.startswith("data:"):
                return sid2, json.loads(ln[5:])
        return sid2, json.loads(raw)
sid, _ = call(None, "initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}, 1)
r.urlopen(r.Request(URL, data=json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"}).encode(), headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream","mcp-session-id":sid}, method="POST"), timeout=5).read()

# Try creating one filter and dump full response
_, resp = call(sid, "tools/call", {
    "name":"gmail_create_filter",
    "arguments":{"add_labels":["Bank Statements","Bank Statements/HSBC Statement"],"from_addr":"ebanking@mail.hsbc.com.sg"}
}, 2)
print("Full response:")
print(json.dumps(resp, indent=2)[:1500])
