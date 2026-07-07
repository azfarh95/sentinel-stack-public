"""Regression test (manual / WSL-integration) for the openclaw_one_shot quoting fix.

Bug (2026-06-16): the turn was invoked as `wsl.exe -- bash -lc "<...message...>"`,
inlining the message with shlex.quote. Windows subprocess (list2cmdline) + wsl.exe
re-parsing STRIP the POSIX single-quoting, so any backtick / $() / ; in the message
(or the history preamble) got executed by bash -> "unexpected EOF while looking for
matching `" -> the whole turn died before the model ran. One ``` code fence in a
thread poisoned every later turn in it; /new (empty history) gave brief relief.

Fix: hand the payload across the wsl boundary OUT-OF-BAND. The message bytes go to a
temp file; a temp script (quoting preserved on disk) reads it into "$MSG" and execs
node; the only thing on the wsl command line is `bash <plain-path>`. This test mirrors
that exact transport (printf instead of `exec node`, so NO model/GPU) and asserts the
message round-trips byte-for-byte through metacharacters.

Run:  python -m openclaw.test_wsl_quoting_transport   (requires WSL + the distro)
"""
import os
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from openclaw.brain_wrapper import WSL_DISTRO, _win_to_wsl  # the REAL helper/const

CASES = {
    "plain": "Hello there",
    "apostrophe": "it's Azfar's, don't fail",
    "backtick": "look at `this`",
    "codefence": "log:\n```\nchat_turn failed\n```\nthx",
    "nasty": ("It's retro arcade forge. Review `logs`?\n```bash\nfor i in `seq 1 5`; "
              'do echo $i; done\n```\n"double", $dollar, \\back, ;semicolon, $(whoami)'),
}


def _transport(message: str) -> subprocess.CompletedProcess:
    """Replicate the patched openclaw_one_shot transport, printf in place of node."""
    msg_fd, msg_win = tempfile.mkstemp(suffix=".txt", prefix="oc_msg_")
    scr_fd, scr_win = tempfile.mkstemp(suffix=".sh", prefix="oc_run_")
    try:
        with os.fdopen(msg_fd, "w", encoding="utf-8", newline="") as f:
            f.write(message)
        script = f"MSG=\"$(cat {shlex.quote(_win_to_wsl(msg_win))})\"\n" 'printf "%s" "$MSG"\n'
        with os.fdopen(scr_fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        cmd = ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc", f"bash {_win_to_wsl(scr_win)}"]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
    finally:
        for p in (msg_win, scr_win):
            try:
                os.unlink(p)
            except OSError:
                pass


def main() -> int:
    ok = True
    for name, msg in CASES.items():
        r = _transport(msg)
        match = r.stdout == msg
        err = (r.stderr or "").strip().replace("\n", " ")[:80]
        ok = ok and match and not err
        print(f"{name:11} {'MATCH' if match else 'MISMATCH':8} rc={r.returncode}"
              + (f"  stderr={err}" if err else ""))
    print("\nALL ROUND-TRIP CLEAN" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
