"""Probe CDP — list targets, then test Page.startScreencast for ~5 seconds
to confirm we can get high-fps frames over the WebSocket."""
import json
import time
import urllib.request

import websocket  # pip install websocket-client


# ── Step 1: list targets ────────────────────────────────────────────────
r = urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=5)
targets = json.loads(r.read())
print('CDP targets:')
page_ws = None
for t in targets:
    print(f'  {t["type"]:10s} title={t.get("title","")[:30]:30s} url={t.get("url","")[:60]}')
    if t.get('type') == 'page' and not page_ws:
        page_ws = t['webSocketDebuggerUrl']

if not page_ws:
    print('No page target found.')
    raise SystemExit(1)

print(f'\nPage WS: {page_ws}\n')


# ── Step 2: connect and test Page.startScreencast ───────────────────────
ws = websocket.create_connection(page_ws, timeout=10)
print('Connected. Enabling Page domain + Input...')

next_id = 0


def send(method, params=None):
    global next_id
    next_id += 1
    msg = {'id': next_id, 'method': method}
    if params:
        msg['params'] = params
    ws.send(json.dumps(msg))


send('Page.enable')
send('Page.navigate', {'url': 'https://example.com'})
send('Page.startScreencast', {
    'format': 'jpeg',
    'quality': 70,
    'maxWidth': 1280,
    'maxHeight': 800,
    'everyNthFrame': 1,
})

print('Sent enable + navigate + startScreencast. Capturing for 6 seconds...\n')

frames = 0
other_methods = {}
start = time.time()
ws.settimeout(0.5)
while time.time() - start < 6.0:
    try:
        raw = ws.recv()
    except websocket.WebSocketTimeoutException:
        continue
    except Exception as e:
        print(f'  recv error: {e}')
        break
    msg = json.loads(raw)
    method = msg.get('method', '')
    if method == 'Page.screencastFrame':
        frames += 1
        params = msg['params']
        if frames <= 3 or frames % 10 == 0:
            print(f'  frame #{frames} at +{time.time()-start:.2f}s: b64_len={len(params.get("data",""))}')
        send('Page.screencastFrameAck', {'sessionId': params['sessionId']})
    elif method:
        other_methods[method] = other_methods.get(method, 0) + 1
    elif 'id' in msg:
        # response to one of our commands
        if msg.get('error'):
            print(f'  cmd id={msg["id"]} ERROR: {msg["error"]}')

elapsed = time.time() - start
print(f'\n=== Results ===')
print(f'Frames received: {frames} ({frames/elapsed:.1f} fps)')
print(f'Other events: {dict(sorted(other_methods.items(), key=lambda x: -x[1])[:8])}')

send('Page.stopScreencast')
ws.close()
