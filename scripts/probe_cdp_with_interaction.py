"""Test CDP screencast under simulated interaction — should see continuous frames
when page is changing, and pause when static (correct behavior)."""
import json
import time
import urllib.request

import websocket


r = urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=5)
page_ws = next(t['webSocketDebuggerUrl'] for t in json.loads(r.read()) if t['type'] == 'page')
print(f'Page WS: {page_ws}')

ws = websocket.create_connection(page_ws, timeout=10)
ws.settimeout(0.3)
nid = 0


def cmd(method, params=None):
    global nid
    nid += 1
    ws.send(json.dumps({'id': nid, 'method': method, 'params': params or {}}))


def drain_short():
    """Drain pending messages, count frames; return count."""
    n = 0
    while True:
        try:
            raw = ws.recv()
        except Exception:
            return n
        msg = json.loads(raw)
        if msg.get('method') == 'Page.screencastFrame':
            n += 1
            cmd('Page.screencastFrameAck', {'sessionId': msg['params']['sessionId']})


cmd('Page.enable')
cmd('Page.navigate', {'url': 'https://example.com'})
cmd('Page.startScreencast', {'format': 'jpeg', 'quality': 70, 'maxWidth': 1280, 'maxHeight': 800, 'everyNthFrame': 1})

print('\n--- Phase 1: nav to example.com, idle 3s ---')
phase1_start = time.time()
phase1_frames = 0
while time.time() - phase1_start < 3.0:
    phase1_frames += drain_short()
print(f'Phase 1 (idle): {phase1_frames} frames in 3s')

print('\n--- Phase 2: scroll repeatedly ---')
phase2_start = time.time()
phase2_frames = 0
scrolls = 0
while time.time() - phase2_start < 3.0:
    cmd('Input.dispatchMouseEvent', {
        'type': 'mouseWheel', 'x': 400, 'y': 300, 'deltaX': 0, 'deltaY': 100
    })
    scrolls += 1
    phase2_frames += drain_short()
    time.sleep(0.1)
print(f'Phase 2 ({scrolls} scrolls): {phase2_frames} frames in 3s = {phase2_frames/3:.1f} fps')

print('\n--- Phase 3: nav to a dynamic page (HN) ---')
cmd('Page.navigate', {'url': 'https://news.ycombinator.com'})
phase3_start = time.time()
phase3_frames = 0
while time.time() - phase3_start < 4.0:
    phase3_frames += drain_short()
print(f'Phase 3 (HN load): {phase3_frames} frames in 4s')

cmd('Page.stopScreencast')
ws.close()
print('\nDone.')
