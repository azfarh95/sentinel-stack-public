"""
Sentinel + Crib Watchdog system tray monitor.
Left circle = Sentinel Watchdog, Right circle = Crib Watchdog.
Green = running, Red = stopped.
"""

import subprocess
import sys
import threading
import time
import webbrowser

from PIL import Image, ImageDraw
import pystray

WATCHDOG_TASK = "Sentinel Watchdog"
CRIB_DIR = os.environ.get(
    "CRIB_WATCHDOG_DIR",
    os.path.join(os.environ.get("USERPROFILE", ""),
                 r".docker\cagent\working_directories\docker-gordon-v7"
                 r"\6f9243c0-aab4-4c98-b86b-d0132dc9bebf\default\crib-watchdog"),
)
CRIB_CORE_CONTAINER = "crib-power-monitor"
HA_URL = "http://localhost:8123"
DASHBOARD_URL = "https://your-domain.example.com"

_NO_WINDOW = subprocess.CREATE_NO_WINDOW
_REFRESH_INTERVAL = 30  # seconds


# ── Status checks ─────────────────────────────────────────────────────────────

def is_sentinel_running() -> bool:
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", WATCHDOG_TASK, "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=6,
            creationflags=_NO_WINDOW,
        )
        return "Running" in r.stdout
    except Exception:
        return False


def is_crib_running() -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", CRIB_CORE_CONTAINER],
            capture_output=True, text=True, timeout=6,
            creationflags=_NO_WINDOW,
        )
        return r.stdout.strip() == "true"
    except Exception:
        return False


# ── Icon drawing ──────────────────────────────────────────────────────────────

_GREEN = (40, 210, 90)
_RED   = (220, 55, 35)
_GREY  = (80, 80, 80)


def make_icon(s_ok: bool, c_ok: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    s_color = _GREEN if s_ok else _RED
    c_color = _GREEN if c_ok else _RED

    # Sentinel circle (left)
    d.ellipse([4, 8, 30, 56], fill=s_color)
    # Crib circle (right)
    d.ellipse([34, 8, 60, 56], fill=c_color)

    # Thin divider
    d.line([(32, 16), (32, 48)], fill=(20, 20, 20, 180), width=2)

    return img


def _status_label(ok: bool) -> str:
    return "Running" if ok else "Stopped"


# ── Actions ───────────────────────────────────────────────────────────────────

def _run(cmd, cwd=None):
    subprocess.Popen(cmd, cwd=cwd, creationflags=_NO_WINDOW)


def action_start_sentinel(icon, _item):
    _run(["schtasks", "/run", "/tn", WATCHDOG_TASK])
    time.sleep(3)
    _refresh_icon(icon)


def action_stop_sentinel(icon, _item):
    _run(["schtasks", "/end", "/tn", WATCHDOG_TASK])
    time.sleep(2)
    _refresh_icon(icon)


def action_start_crib(icon, _item):
    _run(["docker", "compose", "up", "-d"], cwd=CRIB_DIR)
    time.sleep(4)
    _refresh_icon(icon)


def action_stop_crib(icon, _item):
    _run(["docker", "compose", "stop"], cwd=CRIB_DIR)
    time.sleep(3)
    _refresh_icon(icon)


def action_open_ha(_icon, _item):
    webbrowser.open(HA_URL)


def action_open_dashboard(_icon, _item):
    webbrowser.open(DASHBOARD_URL)


# ── Menu (dynamic — rebuilt each open) ───────────────────────────────────────

def _menu_items():
    s_ok = is_sentinel_running()
    c_ok = is_crib_running()
    dot_s = "●" if s_ok else "○"
    dot_c = "●" if c_ok else "○"
    return (
        pystray.MenuItem(
            f"Sentinel Watchdog  {dot_s} {_status_label(s_ok)}",
            None, enabled=False,
        ),
        pystray.MenuItem(
            "  Start Sentinel",
            action_start_sentinel,
            enabled=not s_ok,
        ),
        pystray.MenuItem(
            "  Stop Sentinel",
            action_stop_sentinel,
            enabled=s_ok,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            f"Crib Watchdog  {dot_c} {_status_label(c_ok)}",
            None, enabled=False,
        ),
        pystray.MenuItem(
            "  Start Crib Watchdog",
            action_start_crib,
            enabled=not c_ok,
        ),
        pystray.MenuItem(
            "  Stop Crib Watchdog",
            action_stop_crib,
            enabled=c_ok,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Home Assistant", action_open_ha),
        pystray.MenuItem("Open Sentinel Dashboard", action_open_dashboard),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda icon, _: icon.stop()),
    )


# ── Icon refresh ──────────────────────────────────────────────────────────────

def _refresh_icon(icon: pystray.Icon):
    s_ok = is_sentinel_running()
    c_ok = is_crib_running()
    icon.icon = make_icon(s_ok, c_ok)
    icon.title = f"Sentinel: {_status_label(s_ok)}  |  Crib: {_status_label(c_ok)}"


def _auto_refresh(icon: pystray.Icon):
    while True:
        time.sleep(_REFRESH_INTERVAL)
        try:
            _refresh_icon(icon)
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    s_ok = is_sentinel_running()
    c_ok = is_crib_running()

    icon = pystray.Icon(
        name="watchdog-monitor",
        icon=make_icon(s_ok, c_ok),
        title=f"Sentinel: {_status_label(s_ok)}  |  Crib: {_status_label(c_ok)}",
        menu=pystray.Menu(_menu_items),
    )

    threading.Thread(target=_auto_refresh, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    import ctypes, traceback, pathlib

    # Singleton — only one tray icon at a time
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "WatchdogTrayMonitor_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)

    _log = pathlib.Path(__file__).parent / "tray_monitor.log"
    try:
        main()
    except Exception:
        _log.write_text(traceback.format_exc())
        raise
