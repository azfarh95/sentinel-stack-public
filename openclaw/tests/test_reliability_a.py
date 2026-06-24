"""A-resolution reliability fixes (2026-06-15) — regression tests.

Covers the three code changes that make Dove's turns reliable for real daily
use (the GPU-contention root cause is config, not code):

  * 3.2 — error-WRITE poison stop: a non-ok turn never persists replayable
          assistant content, even when it returned partial reply text.
  * 3.3 — turn-level fence: an exception in the OpenClaw call still finalizes
          the reserved row (no orphan, no held slot); + the orphan reaper.
  * 3.4 — brain_store self-loads .env.local (no-override) so an env-less
          restart still authenticates to Postgres.

The mock-based tests need no DB. The reaper test is a live-DB integration that
creates a throwaway conversation and tears it down in a finally; it SKIPS if
Postgres is unreachable so the suite still runs in a DB-less environment.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import uuid

import pytest

import openclaw.brain_wrapper as bw
from openclaw import brain_store as bs


# ── helpers ──────────────────────────────────────────────────────────────
class _FakeMsg:
    def __init__(self, mid: int):
        self.id = mid


class _FakeStore:
    """Minimal stand-in for BrainStore over the surface chat_turn_finish uses,
    so the poison/fence logic is tested with zero DB and zero OpenClaw."""

    def __init__(self):
        self.finalized: dict | None = None

    def load_for_llm(self, tid, max_tokens=8000, **kw):
        return []  # no history → empty preamble

    def finalize(self, message_id, content, tokens_in=None, tokens_out=None, model=None):
        self.finalized = {
            "message_id": message_id,
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model": model,
        }
        return _FakeMsg(message_id)


@pytest.fixture
def patched_wrapper(monkeypatch):
    """Neutralise the host-wide turnstile (Windows named mutex) so the unit
    tests don't depend on OS mutex behaviour."""
    monkeypatch.setattr(bw, "gateway_turnstile", lambda: contextlib.nullcontext())
    return monkeypatch


def _run_turn(store):
    return bw.chat_turn_finish(
        thread_id=uuid.uuid4(),
        user_msg="hello",
        assistant_message_id=4242,
        store=store,
        timeout_s=600,
    )


# ── 3.2 — error-WRITE poison stop ────────────────────────────────────────
def test_failed_turn_with_partial_text_is_not_persisted_as_replayable(patched_wrapper):
    """The crux leak: a non-ok turn that returned PARTIAL text must be stored
    behind the [bridge_error] sentinel (which the read filter excludes), NOT as
    the bare partial text the content-denylist can't catch."""
    partial = "Here is the half-finished answer that the model emitted before"
    patched_wrapper.setattr(bw, "openclaw_one_shot", lambda **kw: {"_raw": 1})
    patched_wrapper.setattr(
        bw, "extract_reply",
        lambda turn: {"ok": False, "error": "agent_status:error",
                      "detail": "agent returned status='error'", "reply": partial},
    )
    store = _FakeStore()
    result = _run_turn(store)

    content = store.finalized["content"]
    assert content.startswith("[bridge_error]"), content
    assert not content.startswith(partial)          # never raw replayable text
    assert "partial[" in content                     # forensic tail preserved
    assert result["ok"] is False


def test_failed_turn_no_text_uses_bare_sentinel(patched_wrapper):
    patched_wrapper.setattr(bw, "openclaw_one_shot", lambda **kw: {"_raw": 1})
    patched_wrapper.setattr(
        bw, "extract_reply",
        lambda turn: {"ok": False, "error": "bridge_error",
                      "detail": "agent returned status=None", "reply": ""},
    )
    store = _FakeStore()
    _run_turn(store)
    content = store.finalized["content"]
    assert content == "[bridge_error] agent returned status=None"
    assert "partial[" not in content


def test_ok_turn_persists_clean_reply(patched_wrapper):
    patched_wrapper.setattr(bw, "openclaw_one_shot", lambda **kw: {"_raw": 1})
    patched_wrapper.setattr(
        bw, "extract_reply",
        lambda turn: {"ok": True, "reply": "the real answer", "model": "qwen"},
    )
    store = _FakeStore()
    result = _run_turn(store)
    assert store.finalized["content"] == "the real answer"
    assert "[bridge_error]" not in store.finalized["content"]
    assert result["ok"] is True


# ── 3.3 — turn-level fence ───────────────────────────────────────────────
def test_subprocess_timeout_still_finalizes_row(patched_wrapper):
    """The orphan bug: openclaw_one_shot raising TimeoutExpired (the 900s
    HARD_TIMEOUT_S kill) must NOT propagate and leave the reserved row
    in-flight. The fence finalizes it behind a sentinel and returns ok=False."""
    def _boom(**kw):
        raise subprocess.TimeoutExpired(cmd="wsl openclaw agent", timeout=900)
    patched_wrapper.setattr(bw, "openclaw_one_shot", _boom)
    store = _FakeStore()
    result = _run_turn(store)  # must not raise

    assert store.finalized is not None, "reserved row was orphaned, not finalized"
    assert store.finalized["content"].startswith("[bridge_error]")
    assert "TimeoutExpired" in store.finalized["content"]
    assert result["ok"] is False
    assert result["error"] == "turn_exception"


def test_generic_exception_is_fenced(patched_wrapper):
    def _boom(**kw):
        raise RuntimeError("wsl gateway died")
    patched_wrapper.setattr(bw, "openclaw_one_shot", _boom)
    store = _FakeStore()
    result = _run_turn(store)
    assert store.finalized["content"].startswith("[bridge_error]")
    assert result["ok"] is False


# ── 3.4 — brain_store self-loads .env.local (no-override) ─────────────────
def test_load_env_local_fills_missing(tmp_path, monkeypatch):
    env = tmp_path / ".env.local"
    env.write_text(
        'POSTGRES_PASSWORD="s3cr3t"\nPOSTGRES_USER=metamcp_user\n'
        "POSTGRES_PORT=5432\n# comment\nPOSTGRES_DB=metamcp_db\n",
        encoding="utf-8",
    )
    for k in ("POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PORT"):
        monkeypatch.delenv(k, raising=False)
    bs._load_env_local(path=str(env))
    assert os.environ["POSTGRES_PASSWORD"] == "s3cr3t"   # quotes stripped
    assert os.environ["POSTGRES_USER"] == "metamcp_user"
    assert os.environ["POSTGRES_DB"] == "metamcp_db"
    # In-container port is deliberately NOT imported (host must use 9433).
    assert "POSTGRES_PORT" not in os.environ


def test_load_env_local_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env.local"
    env.write_text('POSTGRES_PASSWORD=from_file\n', encoding="utf-8")
    monkeypatch.setenv("POSTGRES_PASSWORD", "from_launcher")
    bs._load_env_local(path=str(env))
    assert os.environ["POSTGRES_PASSWORD"] == "from_launcher"  # launcher wins


def test_load_env_local_missing_file_is_silent(tmp_path):
    bs._load_env_local(path=str(tmp_path / "does-not-exist.env"))  # must not raise


# ── reaper — live DB integration (skips if Postgres unreachable) ──────────
@pytest.fixture
def live_store():
    import psycopg
    store = bs.BrainStore()
    try:
        with psycopg.connect(store.dsn, connect_timeout=5):
            pass
    except Exception as exc:
        pytest.skip(f"Postgres unreachable, skipping live reaper test: {exc}")
    return store


def test_reap_orphans_finalizes_old_not_fresh(live_store):
    import psycopg
    store = live_store
    tname = f"_test_reaper_{uuid.uuid4().hex[:8]}"
    thread = store.create_thread(user_id="_test", name=tname, kind="test")
    tid = thread.id
    try:
        old = store.append(conv_id=tid, role="assistant", content="", streaming_done=False)
        fresh = store.append(conv_id=tid, role="assistant", content="", streaming_done=False)
        # Backdate the "old" orphan well past the 20-min cutoff.
        with psycopg.connect(store.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brain.messages SET created_at = now() - interval '30 minutes' WHERE id = %s",
                (old.id,),
            )
            conn.commit()

        reaped = store.reap_orphans(older_than_minutes=20)
        assert reaped >= 1

        with psycopg.connect(store.dsn, row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT content, streaming_done FROM brain.messages WHERE id = %s", (old.id,))
            o = cur.fetchone()
            cur.execute("SELECT content, streaming_done FROM brain.messages WHERE id = %s", (fresh.id,))
            f = cur.fetchone()
        assert o["streaming_done"] is True and o["content"] == "[interrupted]"
        assert f["streaming_done"] is False, "a fresh in-flight turn must NOT be reaped"
    finally:
        with psycopg.connect(store.dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM brain.messages WHERE conv_id = %s", (tid,))
            cur.execute("DELETE FROM brain.conversations WHERE id = %s", (tid,))
            conn.commit()
