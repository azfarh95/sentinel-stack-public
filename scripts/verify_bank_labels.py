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
_, resp = call(sid, "tools/call", {"name":"gmail_list_labels","arguments":{}}, 2)
content = resp["result"]["content"]
print(f"Content items: {len(content)}")
# Each item is {"type":"text","text":"<json>"} — collect all into a flat list
labels = []
for item in content:
    parsed = json.loads(item["text"])
    if isinstance(parsed, list):
        labels.extend(parsed)
    else:
        labels.append(parsed)
print(f"Total labels: {len(labels)}")
banks = sorted([l for l in labels if "Bank Statements" in l.get("name","")], key=lambda x: x["name"])
print(f"\n=== Bank Statements tree ({len(banks)} labels) ===")
for l in banks:
    print(f"  {l['id']:12}  {l['name']}")
