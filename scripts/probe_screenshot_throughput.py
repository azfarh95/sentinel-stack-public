"""Measure how long each screenshot call takes via MetaMCP, to confirm whether
the 'only 1 frame' issue is rate-limit or session-related."""
import urllib.request
import json
import time

URL = 'http://localhost:12008/metamcp/default/mcp'
TOKEN = 'sk_mt_LiNBl2Mu6yY2WKua5enLo7Za86TWGfxKPt9O2gocLvzpsIe1IfJuwCcVpxteS0At'
H = {
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
    'Authorization': f'Bearer {TOKEN}',
}


def parse_sse(b):
    for line in b.splitlines():
        if line.startswith('data:'):
            return json.loads(line[5:].strip())
    return {}


# Init
init = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
               'clientInfo': {'name': 'p', 'version': '0.1'}}}).encode()
r = urllib.request.urlopen(urllib.request.Request(URL, data=init, headers=H), timeout=15)
sid = r.headers.get('mcp-session-id')
r.read()
notif = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(URL, data=notif, headers={**H, 'mcp-session-id': sid}), timeout=5).read()
except Exception:
    pass

print(f'session: {sid[:12]}...')

for i in range(5):
    t0 = time.time()
    body = json.dumps({'jsonrpc': '2.0', 'id': 99 + i, 'method': 'tools/call',
        'params': {'name': 'Playwright__browser_take_screenshot', 'arguments': {'type': 'jpeg'}}}).encode()
    try:
        r = urllib.request.urlopen(urllib.request.Request(URL, data=body, headers={**H, 'mcp-session-id': sid}), timeout=15)
        result = parse_sse(r.read().decode())
        elapsed = time.time() - t0
        if 'error' in result:
            err = result.get('error', {})
            print(f'  shot{i}: ERROR after {elapsed:.2f}s')
            print(f'    {err}')
        else:
            n_images = sum(1 for x in result.get('result', {}).get('content', []) if x.get('type') == 'image')
            jpeg_size = sum(len(x.get('data', '')) for x in result.get('result', {}).get('content', []) if x.get('type') == 'image')
            text_content = ''
            for x in result.get('result', {}).get('content', []):
                if x.get('type') == 'text':
                    text_content = x.get('text', '')[:80]
                    break
            print(f'  shot{i}: {elapsed:.2f}s, {n_images} image(s), b64={jpeg_size} chars')
            if text_content:
                print(f'    text: {text_content}')
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ''
        print(f'  shot{i}: HTTP {e.code} after {time.time()-t0:.2f}s - {body[:200]}')
    except Exception as e:
        print(f'  shot{i}: EXCEPTION after {time.time()-t0:.2f}s - {type(e).__name__}: {e}')
    time.sleep(0.5)
