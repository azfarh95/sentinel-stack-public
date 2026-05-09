"""Probe whether MetaMCP routes browser_take_screenshot to the SAME Playwright
session the agent uses, or a per-client one. If same: bridge can use this. If
different: need a different approach (shared user-data-dir, etc.)."""
import urllib.request
import json

URL = 'http://localhost:12008/metamcp/default/mcp'
TOKEN = 'sk_mt_LiNBl2Mu6yY2WKua5enLo7Za86TWGfxKPt9O2gocLvzpsIe1IfJuwCcVpxteS0At'
H = {
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
    'Authorization': f'Bearer {TOKEN}',
}


def parse_sse(body):
    for line in body.splitlines():
        if line.startswith('data:'):
            return json.loads(line[5:].strip())
    return {}


def init():
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                   'clientInfo': {'name': 'sentinel-bridge-probe', 'version': '0.1'}}}).encode()
    req = urllib.request.Request(URL, data=body, headers=H)
    r = urllib.request.urlopen(req, timeout=15)
    sid = r.headers.get('mcp-session-id')
    r.read()
    notif = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(URL, data=notif, headers={**H, 'mcp-session-id': sid}), timeout=5).read()
    except Exception:
        pass
    return sid


def call(sid, method, params=None, t_id=99):
    payload = {'jsonrpc': '2.0', 'id': t_id, 'method': method}
    if params:
        payload['params'] = params
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                  headers={**H, 'mcp-session-id': sid})
    r = urllib.request.urlopen(req, timeout=30)
    return parse_sse(r.read().decode())


sid = init()
print('MetaMCP session:', sid)

# Find playwright screenshot tool name through MetaMCP (it may have a namespace prefix)
tools = call(sid, 'tools/list').get('result', {}).get('tools', [])
print(f'\nTools via MetaMCP: {len(tools)}')
shot_tool = None
for t in tools:
    name = t['name']
    if 'screenshot' in name.lower():
        print(f'  SHOT: {name}')
        shot_tool = name
    elif 'navigate' in name.lower() and 'browser' in name.lower():
        print(f'  NAV : {name}')

if not shot_tool:
    print('No screenshot tool exposed through MetaMCP!')
    # show what playwright tools ARE there
    print('\nPlaywright-ish tools:')
    for t in tools:
        if 'browser' in t['name'].lower() or 'playwright' in t['name'].lower():
            print(f'  {t["name"]}')
else:
    print(f'\n--- Trying {shot_tool} via MetaMCP ---')
    try:
        result = call(sid, 'tools/call', {'name': shot_tool, 'arguments': {'type': 'jpeg'}}, t_id=200)
        if 'error' in result:
            print(f'  ERROR: {result["error"]}')
        else:
            content = result.get('result', {}).get('content', [])
            for item in content[:3]:
                t = item.get('type', '?')
                if t == 'image':
                    data = item.get('data', '')
                    print(f'  IMAGE bytes_b64={len(data)}, mime={item.get("mimeType","?")}')
                else:
                    print(f'  {t}: {(item.get("text","") or json.dumps(item))[:300]}')
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ''
        print(f'  HTTP {e.code}: {body[:300]}')
    except Exception as e:
        print(f'  EXCEPTION {type(e).__name__}: {e}')
