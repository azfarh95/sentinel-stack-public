"""Trigger a navigate via Playwright MCP, then immediately introspect the
Chromium process Playwright spawned to find its CDP debug port."""
import urllib.request
import json
import subprocess
import time
import re

URL = 'http://127.0.0.1:8932/mcp'
H = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}


def parse_sse(b):
    for line in b.splitlines():
        if line.startswith('data:'):
            return json.loads(line[5:].strip())
    return {}


# 1) Init MCP session
init = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
               'clientInfo': {'name': 'cdp-probe', 'version': '0.1'}}}).encode()
r = urllib.request.urlopen(urllib.request.Request(URL, data=init, headers=H), timeout=15)
sid = r.headers.get('mcp-session-id')
r.read()
notif = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(URL, data=notif, headers={**H, 'mcp-session-id': sid}), timeout=5).read()
except Exception:
    pass

# 2) Navigate (this launches Chromium if not already running)
print('Triggering browser_navigate...')
nav = json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
    'params': {'name': 'browser_navigate', 'arguments': {'url': 'https://example.com'}}}).encode()
r = urllib.request.urlopen(urllib.request.Request(URL, data=nav, headers={**H, 'mcp-session-id': sid}), timeout=30)
result = parse_sse(r.read().decode())
text = ''
for item in result.get('result', {}).get('content', []):
    if item.get('type') == 'text':
        text = item.get('text', '')
        break
print('Nav result first 300 chars:', text[:300].replace('\n', ' | '))

# 3) Find the Chromium that just spawned + its CDP port
print('\nLooking for spawned Chromium...')
time.sleep(2)
ps = subprocess.run(['powershell', '-NoProfile', '-Command',
    'Get-CimInstance Win32_Process | Where-Object { $_.Name -in @("chrome.exe","chromium.exe","msedge.exe") } | Select-Object ProcessId, CommandLine | Format-List'],
    capture_output=True, text=True, timeout=10)

found_port = None
for chunk in ps.stdout.split('ProcessId :'):
    chunk = chunk.strip()
    if not chunk:
        continue
    pid_match = re.match(r'(\d+)', chunk)
    if not pid_match:
        continue
    pid = pid_match.group(1)
    port_match = re.search(r'remote-debugging-port=(\d+)', chunk)
    type_match = re.search(r'--type=(\w+)', chunk)
    if port_match:
        port = port_match.group(1)
        print(f'  PID {pid} [main] : remote-debugging-port={port}')
        found_port = port
    elif type_match:
        print(f'  PID {pid} [{type_match.group(1)}]')
    else:
        print(f'  PID {pid} [main, no debug port found]')

# 4) If we got a port, hit /json/version on it
if found_port:
    print(f'\nProbing CDP at http://127.0.0.1:{found_port}/json/version ...')
    try:
        r = urllib.request.urlopen(f'http://127.0.0.1:{found_port}/json/version', timeout=3)
        info = json.loads(r.read())
        print(f'  Browser: {info.get("Browser","?")}')
        print(f'  WS URL: {info.get("webSocketDebuggerUrl","?")[:100]}')
    except Exception as e:
        print(f'  ERROR: {e}')
    print(f'\nProbing CDP /json (page list) ...')
    try:
        r = urllib.request.urlopen(f'http://127.0.0.1:{found_port}/json', timeout=3)
        pages = json.loads(r.read())
        for p in pages[:3]:
            print(f'  page: {p.get("title","?")[:40]} | {p.get("url","?")[:60]}')
    except Exception as e:
        print(f'  ERROR: {e}')
else:
    print('\nNo debug port found on any Chromium process.')
