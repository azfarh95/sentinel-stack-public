"""P4.5 — persist a completed browser turn.

Always appends to a local `browser_turns.jsonl` (surface log), and best-effort
mirrors it into the OpenClaw brain_store (surface='browser', own thread) by
shelling out to the SYSTEM python (`_brain_persist.py`) — the browser venv has no
psycopg/openclaw, the system one does. Never raises; persistence must not break a run.
"""
import json
import os
import subprocess
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TURNS = _HERE / "browser_turns.jsonl"
_WRITER = _HERE / "_brain_persist.py"
_SYS_PY = os.environ.get("SYSTEM_PYTHON") or \
    r"C:\Users\azfar\AppData\Local\Programs\Python\Python312\python.exe"

_FIELDS = ("task", "status", "final", "steps", "dur_s", "gated", "use_vision", "model")


def persist_turn(rec: dict, *, brain: bool = True) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "surface": "browser"}
    row.update({k: rec.get(k) for k in _FIELDS})
    try:
        with open(_TURNS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    if brain and _WRITER.exists() and Path(_SYS_PY).exists():
        try:
            # CREATE_NO_WINDOW: don't flash a console window every turn (python.exe is a
            # console app; without this it pops a terminal on Windows — disruptive). 0 on non-Win.
            subprocess.run([_SYS_PY, str(_WRITER)], input=json.dumps(rec).encode("utf-8"),
                           timeout=20, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
