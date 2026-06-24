"""Write ONE browser turn to the OpenClaw brain_store — surface='browser', its own
thread ('browser-assistant'), NOT fused into the DM thread (the C-continuity rule).

Run by the SYSTEM python (which has psycopg + the openclaw package); the browser
venv shells out to it (its own venv has neither). Fed the turn record as JSON on
stdin. Best-effort: exit 0 on success, 1 on any error (caller ignores failures).
"""
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # metamcp-local
sys.path.insert(0, str(_ROOT))               # so brain_store's internal `from openclaw import …` resolves
sys.path.insert(0, str(_ROOT / "openclaw"))  # so `import brain_store` (top-level) resolves


def main() -> int:
    try:
        rec = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 1
    task = (rec.get("task") or "").strip()
    if not task:
        return 1
    try:
        import brain_store
        bs = brain_store.BrainStore()
        th = bs.get_or_create_default(name="browser-assistant", kind="browser")
        bs.append(th.id, "user", task, surface="browser")
        final = rec.get("final") or rec.get("err") or "no result"
        body = (f"[{rec.get('status')}] {final}\n"
                f"(steps={rec.get('steps')} dur={rec.get('dur_s')}s gated={rec.get('gated')})")
        bs.append(th.id, "assistant", body, surface="browser",
                  model=rec.get("model") or "qwen/qwen3.6-27b")
        print("brain ok:", th.id)
        return 0
    except Exception as e:  # noqa: BLE001
        print("brain persist failed:", type(e).__name__, str(e)[:200], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
