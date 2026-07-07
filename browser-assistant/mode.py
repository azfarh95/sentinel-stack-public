"""P4.5 kill-switch — browser mode is OFF while `.browser_disabled` exists.

A file marker (not env) so it toggles live without restarting the surface:
disabling instantly makes the CLI + the :8108 surface refuse new runs, leaving
chat / the rest of the stack untouched.
"""
from pathlib import Path

_FLAG = Path(__file__).resolve().parent / ".browser_disabled"


def browser_enabled() -> bool:
    return not _FLAG.exists()


def set_enabled(on: bool) -> None:
    if on:
        _FLAG.unlink(missing_ok=True)
    else:
        _FLAG.write_text("browser mode disabled\n", encoding="utf-8")


if __name__ == "__main__":
    import sys
    arg = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if arg in ("on", "enable"):
        set_enabled(True); print("browser mode: ENABLED")
    elif arg in ("off", "disable"):
        set_enabled(False); print("browser mode: DISABLED (kill-switch on)")
    else:
        print("browser mode:", "ENABLED" if browser_enabled() else "DISABLED")
