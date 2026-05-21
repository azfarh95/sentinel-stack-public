"""Sample 20 CDP screencast frames during a navigation to measure actual
frame sizes — to dial in everyNthFrame, quality, and dimensions for mobile."""
import json
import time
import urllib.request

import websocket


r = urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=5)
page_ws = next(t['webSocketDebuggerUrl'] for t in json.loads(r.read()) if t['type'] == 'page')
ws = websocket.create_connection(page_ws, timeout=10)
ws.settimeout(0.5)
nid = 0


def cmd(method, params=None):
    global nid
    nid += 1
    ws.send(json.dumps({'id': nid, 'method': method, 'params': params or {}}))


def measure(quality, max_w, max_h, every_nth, label, duration=4.0):
    cmd('Page.stopScreencast')
    time.sleep(0.3)
    # Drain any pending
    try:
        while True: ws.recv()
    except Exception: pass

    print(f'\n=== {label}: q={quality} {max_w}x{max_h} everyNth={every_nth} ===')
    cmd('Page.navigate', {'url': 'https://news.ycombinator.com'})
    cmd('Page.startScreencast', {
        'format': 'jpeg', 'quality': quality,
        'maxWidth': max_w, 'maxHeight': max_h, 'everyNthFrame': every_nth,
    })
    sizes = []
    start = time.time()
    while time.time() - start < duration:
        try:
            raw = ws.recv()
        except Exception:
            continue
        msg = json.loads(raw)
        if msg.get('method') == 'Page.screencastFrame':
            data = msg['params'].get('data', '')
            sizes.append(len(data))  # base64 length
            cmd('Page.screencastFrameAck', {'sessionId': msg['params']['sessionId']})
    if sizes:
        avg_b64 = sum(sizes) / len(sizes)
        avg_raw = avg_b64 * 0.75  # b64 → raw bytes
        total_kb = sum(sizes) / 1024
        fps = len(sizes) / duration
        print(f'  frames: {len(sizes)} in {duration}s = {fps:.1f} fps')
        print(f'  avg size: {avg_raw/1024:.1f} KB raw, {avg_b64/1024:.1f} KB b64')
        print(f'  total wire: {total_kb:.1f} KB / {duration}s = {(total_kb*8)/(duration*1024):.2f} Mbps')
    else:
        print('  no frames received')


cmd('Page.enable')

# Test combinations
measure(70, 1280, 800, 1,  'Current default (no throttle)')
measure(50, 720,  480, 3,  'Mobile-optimised v1')
measure(40, 600,  400, 4,  'Aggressive mobile')
measure(60, 960,  640, 2,  'WiFi balanced')

cmd('Page.stopScreencast')
ws.close()
