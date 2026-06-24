"""Refresh the dynamic part of the infrastructure inventory.

Writes workspace/inventory/running.yaml with the *live* state of the stack:
docker containers, LM Studio loaded models, native processes, listening ports,
scheduled tasks, MetaMCP namespace counts, WSL services.

Static knowledge (architecture decisions, dependency graph, violations tracker)
lives in architecture.yaml and violations.yaml — those are hand-maintained.

Run manually:
    python scripts/refresh_inventory.py

Or hit the bridge endpoint (which calls this in-process):
    POST http://127.0.0.1:8098/api/inventory/refresh
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT     = Path(__file__).resolve().parent.parent
INVENTORY_DIR = REPO_ROOT / "workspace" / "inventory"
RUNNING_YAML  = INVENTORY_DIR / "running.yaml"


def _run(cmd, timeout=10, shell=False):
    """Run a command, return stdout or empty string on any failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, shell=shell)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def docker_containers():
    out = _run(["docker", "ps", "--format",
                "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"])
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        name, image, status, ports = parts
        # Strip "Up X hours (healthy)" → just the duration + state
        m = re.match(r"Up\s+(\S+\s+\S+)\s*(\(\w+\))?", status)
        rows.append({
            "name":   name,
            "image":  image,
            "uptime": m.group(1) if m else status,
            "health": (m.group(2)[1:-1] if m and m.group(2) else "unknown"),
            "ports":  ports[:80],  # truncate massive port lists
        })
    return rows


def lms_models():
    out = _run([str(Path(os.environ.get("USERPROFILE", "")) / ".lmstudio" / "bin" / "lms.exe"),
                "ps", "--json"])
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [
        {
            "model":   m.get("modelKey", ""),
            "context": m.get("contextLength"),
            "parallel": m.get("parallel"),
            "status":  m.get("status"),
            "vision":  bool(m.get("vision")),
            "tools":   bool(m.get("trainedForToolUse")),
        }
        for m in data if m.get("type") == "llm"
    ]


def native_processes():
    """Long-running Python/Node processes belonging to our stack."""
    out = _run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='node.exe'\" | "
         "Where-Object { $_.CommandLine -match 'metamcp-local|sentinel|watchdog|infer_bridge|playwright-mcp|memory.exe|tray_monitor' } | "
         "Select-Object Name, ProcessId, CommandLine | ConvertTo-Json -Compress"],
        timeout=15)
    if not out.strip():
        return []
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
    except json.JSONDecodeError:
        return []
    rows = []
    for p in data:
        cmd = (p.get("CommandLine") or "")
        # Extract just the script name being run
        script = "?"
        for token in cmd.split():
            t = token.strip('"')
            if t.endswith((".py", ".js")) or t.endswith(".exe"):
                script = Path(t).name
        rows.append({
            "name":  p.get("Name"),
            "pid":   p.get("ProcessId"),
            "script": script,
        })
    return rows


def listening_ports():
    """Loopback-only ports in our stack's typical range (1234, 80xx, 90xx, 12xxx, 18xxx)."""
    out = _run(["netstat", "-ano"])
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in line:
            continue
        addr = parts[1]
        pid_ = parts[-1]
        m = re.search(r":(\d+)$", addr)
        if not m:
            continue
        port = int(m.group(1))
        if not (port == 1234 or 8000 <= port < 9999 or 12000 <= port < 13000
                or 18000 <= port < 19000 or port in (5050, 5432, 6379, 9433, 9222)):
            continue
        if not addr.startswith(("127.0.0.1:", "0.0.0.0:")):
            continue
        rows.append({"port": port, "addr": addr, "pid": int(pid_)})
    return sorted({(r["port"], r["addr"], r["pid"]) for r in rows})


def scheduled_tasks():
    out = _run(
        ["powershell", "-NoProfile", "-Command",
         "Get-ScheduledTask | Where-Object { $_.TaskName -match 'Playwright|Sentinel|Watchdog|Tray|Backup' } | "
         "Select-Object TaskName, State | ConvertTo-Json -Compress"],
        timeout=15)
    if not out.strip():
        return []
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
    except json.JSONDecodeError:
        return []
    return [{"name": t.get("TaskName"), "state": str(t.get("State"))} for t in data]


def metamcp_summary():
    out = _run(["docker", "exec", "metamcp-pg", "psql", "-U", "metamcp_user",
                "-d", "metamcp_db", "-t", "-c",
                "SELECT 'namespaces:'||count(*) FROM namespaces "
                "UNION ALL "
                "SELECT type||':'||count(*) FROM mcp_servers GROUP BY type"])
    info = {}
    for line in out.strip().splitlines():
        parts = line.strip().split(":")
        if len(parts) == 2:
            info[parts[0].strip()] = int(parts[1].strip())
    return info


def wsl_services():
    out = _run(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c",
                "systemctl list-units --type=service --state=running --no-pager 2>&1 | "
                "grep -iE 'openclaw|cloudflared|claude' | awk '{print $1\"|\"$NF}'"],
               timeout=10)
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("|", 1)
        if len(parts) == 2:
            rows.append({"unit": parts[0].strip(), "desc": parts[1].strip()[:80]})
    return rows


def script_counts():
    base = REPO_ROOT
    counts = {}
    for sub in ("scripts", "sentinel-miniapp-v2", "watchdog"):
        d = base / sub
        if not d.is_dir():
            continue
        ext_map = {}
        for f in d.rglob("*"):
            if f.is_file() and f.suffix:
                ext = f.suffix.lower()
                ext_map[ext] = ext_map.get(ext, 0) + 1
        counts[sub] = ext_map
    return counts


def _yaml_dump(obj, indent=0):
    """Minimal YAML emitter — avoids adding pyyaml as a dep."""
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_yaml_dump(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return f"{pad}[]"
        lines = []
        for item in obj:
            if isinstance(item, dict):
                lines.append(f"{pad}- " + _yaml_dump(item, indent + 1).lstrip())
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_scalar(obj)}"


def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if not s:
        return '""'
    if any(c in s for c in ":#&*!|>'\"%@`") or s.startswith(("- ", "[", "{")):
        return f'"{s}"'
    return s


def main():
    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "snapshot_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": {},
    }

    print("[inventory] querying docker...")
    snapshot["docker_containers"] = docker_containers()
    snapshot["summary"]["docker_count"] = len(snapshot["docker_containers"])

    print("[inventory] querying LM Studio...")
    snapshot["lm_studio_loaded"] = lms_models()
    snapshot["summary"]["lm_models_loaded"] = len(snapshot["lm_studio_loaded"])

    print("[inventory] querying native processes...")
    snapshot["native_processes"] = native_processes()
    snapshot["summary"]["native_process_count"] = len(snapshot["native_processes"])

    print("[inventory] querying ports...")
    ports = listening_ports()
    snapshot["listening_ports"] = [{"port": p, "addr": a, "pid": pid} for p, a, pid in ports]

    print("[inventory] querying scheduled tasks...")
    snapshot["scheduled_tasks"] = scheduled_tasks()

    print("[inventory] querying MetaMCP postgres...")
    snapshot["metamcp"] = metamcp_summary()

    print("[inventory] querying WSL services...")
    snapshot["wsl_services"] = wsl_services()

    print("[inventory] counting scripts...")
    snapshot["script_counts"] = script_counts()

    yaml_text = "# Auto-generated — do not edit. Regenerate via scripts/refresh_inventory.py\n"
    yaml_text += _yaml_dump(snapshot) + "\n"
    RUNNING_YAML.write_text(yaml_text, encoding="utf-8")

    print(f"\n[inventory] wrote {RUNNING_YAML}")
    print(f"  docker: {snapshot['summary']['docker_count']} containers")
    print(f"  lm models loaded: {snapshot['summary']['lm_models_loaded']}")
    print(f"  native processes: {snapshot['summary']['native_process_count']}")
    print(f"  ports: {len(snapshot['listening_ports'])}")
    print(f"  scheduled tasks: {len(snapshot['scheduled_tasks'])}")
    print(f"  metamcp: {snapshot['metamcp']}")


if __name__ == "__main__":
    main()
