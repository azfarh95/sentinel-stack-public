"""infer-bridge axis-3 2.3 tests — backend-readiness probe + single-flight gate.

Covers the new `_backend_state()` (the :1234/running readiness classifier + its
cache) and the single-flight warm lock. The full _proxy cold path is HTTP-handler
integration (verified at deploy-smoke); the decision-critical pure logic is here.
"""
from __future__ import annotations
import json
import pytest

import infer_bridge as b


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._b


class _Conn:
    """Scriptable stand-in for http.client.HTTPConnection."""
    instances = 0

    def __init__(self, status=200, body=None, raise_exc=None):
        _Conn.instances += 1
        self._status = status
        self._body = body if body is not None else {"running": []}
        self._raise = raise_exc

    def request(self, *a, **k):
        if self._raise:
            raise self._raise

    def getresponse(self):
        return _Resp(self._status, self._body)

    def close(self):
        pass


def _patch_conn(monkeypatch, **conn_kwargs):
    _Conn.instances = 0
    monkeypatch.setattr(b.http.client, "HTTPConnection",
                        lambda *a, **k: _Conn(**conn_kwargs))


@pytest.fixture(autouse=True)
def _reset_cache():
    b._backend_state_cache = ""
    b._backend_state_expires = 0.0
    yield


# ── _backend_state classification ─────────────────────────────────────────────
def test_state_ready(monkeypatch):
    _patch_conn(monkeypatch, body={"running": [{"model": "qwen", "state": "ready"}]})
    assert b._backend_state(force=True) == "ready"


def test_state_loading(monkeypatch):
    _patch_conn(monkeypatch, body={"running": [{"model": "qwen", "state": "starting"}]})
    assert b._backend_state(force=True) == "loading"


def test_state_down_when_no_model(monkeypatch):
    _patch_conn(monkeypatch, body={"running": []})
    assert b._backend_state(force=True) == "down"


def test_state_down_when_unreachable(monkeypatch):
    _patch_conn(monkeypatch, raise_exc=ConnectionRefusedError("refused"))
    assert b._backend_state(force=True) == "down"


def test_state_down_on_non_200(monkeypatch):
    _patch_conn(monkeypatch, status=503, body=b"")
    assert b._backend_state(force=True) == "down"


def test_state_is_cached(monkeypatch):
    _patch_conn(monkeypatch, body={"running": [{"state": "ready"}]})
    assert b._backend_state() == "ready"
    assert b._backend_state() == "ready"        # second call within TTL
    assert _Conn.instances == 1                  # only ONE probe (cache hit)


def test_force_bypasses_cache(monkeypatch):
    _patch_conn(monkeypatch, body={"running": [{"state": "ready"}]})
    b._backend_state()
    b._backend_state(force=True)
    assert _Conn.instances == 2


# ── single-flight warm lock ───────────────────────────────────────────────────
def test_warm_lock_is_single_flight():
    got1 = b._warm_lock.acquire(blocking=False)
    got2 = b._warm_lock.acquire(blocking=False)   # second warmer blocked
    try:
        assert got1 is True
        assert got2 is False
    finally:
        if got1:
            b._warm_lock.release()
    # released → next warmer can acquire
    got3 = b._warm_lock.acquire(blocking=False)
    assert got3 is True
    b._warm_lock.release()
