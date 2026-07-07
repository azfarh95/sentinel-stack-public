"""Trigger a Playwright MCP navigate, then introspect the Chromium process
to find its --remote-debugging-port. From that we can hit /json/version to
get the WebSocket debugger URL — which is what we need for Option A
(direct CDP screencast)."""
import json
import re
import subprocess
import time
import urllib.request

URL = 'http://127.0.0.1:8932/mcp'
H = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}


def parse_sse(b):
    for line in b.splitlines():
        if line.startswith('data:'):
            return json.loads(line[5:].strip())
    return {}


# 1) Trigger a navigate so Chromium spawns
print('Triggering navigate...')
init = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
               'clientInfo': {'name': 'cdp-finder', 'version': '0.1'}}}).encode()
r = urllib.request.urlopen(urllib.request.Request(URL, data=init, headers=H), timeout=15)
sid = r.headers.get('mcp-session-id')
r.read()
notif = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(URL, data=notif, headers={**H, 'mcp-session-id': sid}), timeout=5).read()
except Exception:
    pass
nav = json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
    'params': {'name': 'browser_navigate', 'arguments': {'url': 'about:blank'}}}).encode()
urllib.request.urlopen(urllib.request.Request(URL, data=nav, headers={**H, 'mcp-session-id': sid}), timeout=30).read()
time.sleep(2)

# 2) Find Chromium with --remote-debugging-port in cmdline
print('\nSearching for Chromium...')
ps_cmd = r'''Get-CimInstance Win32_Process | Where-Object { $_.Name -in @("chrome.exe","chromium.exe","msedge.exe") -and $_.CommandLine -like "*remote-debugging*" } | ForEach-Object { "$($_.ProcessId)|$($_.CommandLine)" }'''
ps = subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd],
                    capture_output=True, text=True, timeout=10)

found_port = None
for line in (ps.stdout or '').splitlines():
    if '|' not in line:
        continue
    pid, cmd = line.split('|', 1)
    m = re.search(r'--remote-debugging-port=(\d+)', cmd)
    if m:
        port = m.group(1)
        # Skip "type=" subprocesses; we want the main browser process
        if '--type=' not in cmd:
            print(f'  PID {pid} [main] → port {port}')
            found_port = port
        else:
            t = re.search(r'--type=(\w+)', cmd)
            print(f'  PID {pid} [{t.group(1) if t else "sub"}] → port {port}')

if not found_port:
    print('\nNo Chromium with --remote-debugging-port found. Trying common ports...')
    for p in [9222, 9223, 9224]:
        try:
            r = urllib.request.urlopen(f'http://127.0.0.1:{p}/json/version', timeout=2)
            print(f'  :{p} responds!')
            found_port = str(p)
            break
        except Exception:
            pass

# 3) Hit /json/version on found port
if found_port:
    print(f'\nProbing CDP at http://127.0.0.1:{found_port}/json/version ...')
    try:
        r = urllib.request.urlopen(f'http://127.0.0.1:{found_port}/json/version', timeout=5)
        info = json.loads(r.read())
        print(f'  Browser: {info.get("Browser","?")}')
        print(f'  Browser WS URL: {info.get("webSocketDebuggerUrl","?")}')
    except Exception as e:
        print(f'  ERROR: {e}')

    print(f'\nList of CDP targets at http://127.0.0.1:{found_port}/json ...')
    try:
        r = urllib.request.urlopen(f'http://127.0.0.1:{found_port}/json', timeout=5)
        targets = json.loads(r.read())
        for t in targets[:5]:
            print(f'  type={t.get("type"):10} title={t.get("title","")[:30]:30} url={t.get("url","")[:60]}')
            if t.get('type') == 'page':
                print(f'    Page WS URL: {t.get("webSocketDebuggerUrl","?")}')
    except Exception as e:
        print(f'  ERROR: {e}')
else:
    print('\nNo CDP endpoint found. Will need to launch Chromium ourselves.')
