"""Inspect log entry timestamp fields."""
import io, json, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Open WSL log via UNC path
LOG = r"\\wsl.localhost\Ubuntu-24.04\tmp\openclaw\openclaw-2026-05-10.log"
with open(LOG, encoding="utf-8") as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}")
print()

# Last 5
print("Last 5 entries:")
for line in lines[-5:]:
    try: d = json.loads(line)
    except: continue
    print(f"  time:    {d.get('time', 'X')}")
    print(f"  ts:      {d.get('ts', 'X')}")
    md = d.get('_meta', {}).get('date', 'X')
    print(f"  _meta.date: {md}")
    print(f"  message: {(d.get('message','') or '')[:100]}")
    print()

# Telegram sendMessage events after 18:27 UTC (= 02:27 SGT)
print("Telegram sendMessage events at SGT >= 02:27 (look at time field):")
sent_count = 0
for line in lines:
    try: d = json.loads(line)
    except: continue
    t = d.get("time", "")
    if t < "2026-05-10T02:27:00": continue
    msg = d.get("message", "")
    if "sendMessage" in msg:
        sent_count += 1
        if sent_count <= 10:
            print(f"  {t[:19]}  {msg[:80]}")
print(f"Total sendMessage events: {sent_count}")
