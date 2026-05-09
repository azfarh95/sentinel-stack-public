"""Quick probe of Playwright MCP — list tools, try screenshot."""
import urllib.request, json, sys

URL = 'http://127.0.0.1:8932/mcp'
H = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}


def parse_sse(body):
    for line in body.splitlines():
        if line.startswith('data:'):
            return json.loads(line[5:].strip())
    return None


def init():
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                   'clientInfo': {'name': 'probe', 'version': '0.1'}}}).encode()
    req = urllib.request.Request(URL, data=body, headers=H)
    r = urllib.request.urlopen(req, timeout=10)
    sid = r.headers.get('mcp-session-id')
    r.read()
    # Required: send initialized notification before any tool calls
    notif = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode()
    req2 = urllib.request.Request(URL, data=notif, headers={**H, 'mcp-session-id': sid})
    try:
        urllib.request.urlopen(req2, timeout=5).read()
    except Exception:
        pass  # notifications often return empty / 202
    return sid


def call(sid, method, params=None):
    payload = {'jsonrpc': '2.0', 'id': 99, 'method': method}
    if params:
        payload['params'] = params
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                  headers={**H, 'mcp-session-id': sid})
    r = urllib.request.urlopen(req, timeout=15)
    return parse_sse(r.read().decode())


sid = init()
print('Session:', sid)

result = call(sid, 'tools/list')
tools = result.get('result', {}).get('tools', [])
print(f'\nTools ({len(tools)}):')
for t in tools:
    name = t['name']
    desc = (t.get('description') or '')[:70].replace('\n', ' ')
    print(f'  {name}: {desc}')

# Find a screenshot/snapshot tool
shot_name = None
for t in tools:
    if 'screenshot' in t['name'].lower() or 'snapshot' in t['name'].lower():
        shot_name = t['name']
        break

if shot_name:
    print(f'\nTrying {shot_name}...')
    try:
        result = call(sid, 'tools/call', {'name': shot_name, 'arguments': {}})
        content = result.get('result', {}).get('content', [])
        for item in content:
            t = item.get('type', '?')
            if t == 'image':
                data = item.get('data', '')
                print(f'  IMAGE: type={item.get("mimeType","?")}, b64_len={len(data)}, first20={data[:20]}')
            elif t == 'text':
                print(f'  TEXT: {item.get("text","")[:200]}')
            else:
                print(f'  {t}: {json.dumps(item)[:200]}')
        if result.get('result', {}).get('isError'):
            print('  ERROR returned by tool')
    except Exception as e:
        print(f'  EXCEPTION: {type(e).__name__}: {e}')
