"""Create 5 Gmail filter rules for SG bank statement senders.
Each filter applies BOTH the parent label (Bank Statements) and the bank-specific
child label, so the user gets aggregated and per-bank views.
Then backfills existing matching emails."""
import io, json, sys, urllib.request as r
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

URL = "http://127.0.0.1:8089/mcp"

# (bank, criteria_dict, child_label_name)
BANKS = [
    ("HSBC",    {"from_addr": "ebanking@mail.hsbc.com.sg"},                                "Bank Statements/HSBC Statement"),
    ("DBS",     {"from_addr": "ibanking.alert@dbs.com"},                                   "Bank Statements/DBS Statement"),
    ("Maybank", {"query": "from:SG.estatement@maybank.com OR from:CardsSTMT@maybank.com"}, "Bank Statements/Maybank Statement"),
    ("SC",      {"from_addr": "alerts.sg@sc.com"},                                         "Bank Statements/SC Statement"),
    ("UOB",     {"from_addr": "unialerts@uobgroup.com"},                                   "Bank Statements/UOB Statement"),
]

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

# Init
sid, _ = call(None, "initialize",
              {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}, 1)
r.urlopen(r.Request(URL, data=json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"}).encode(),
                    headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream",
                             "mcp-session-id":sid}, method="POST"), timeout=5).read()

print("=== Creating filters (2 per bank — Gmail allows only 1 label per filter) ===\n")
filter_ids = []
mid = 2
for bank, crit, child in BANKS:
    crit_str = crit.get("from_addr") or crit.get("query")
    for label_name, role in [(child, "child"), ("Bank Statements", "parent")]:
        args = {"add_labels": [label_name], **crit}
        _, resp = call(sid, "tools/call", {"name":"gmail_create_filter","arguments":args}, mid)
        mid += 1
        text = resp.get("result", {}).get("content", [{}])[0].get("text", "")
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"raw": text}
        fid = parsed.get("id", "ERR")
        if "alreadyExists" in text:
            fid = "DUP-OK"
        filter_ids.append((bank, role, fid))
        print(f"  {bank:8} [{role:6}]  filter={fid}  ← {label_name}")

print(f"\n=== Backfilling existing emails ===\n")
mid = 100
for bank, crit, child in BANKS:
    if "from_addr" in crit:
        query = f"from:{crit['from_addr']}"
    else:
        query = crit["query"]
    # Apply parent label
    _, resp = call(sid, "tools/call",
                   {"name":"gmail_apply_label_to_query",
                    "arguments":{"label":"Bank Statements","query":query,"max_results":500}}, mid); mid += 1
    p = json.loads(resp["result"]["content"][0]["text"])
    # Apply child label
    _, resp = call(sid, "tools/call",
                   {"name":"gmail_apply_label_to_query",
                    "arguments":{"label":child,"query":query,"max_results":500}}, mid); mid += 1
    c = json.loads(resp["result"]["content"][0]["text"])
    print(f"  {bank:8}  {p.get('applied','?'):>4} emails → {child}")

print("\nDone.")
