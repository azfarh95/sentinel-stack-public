"""P6+ — in-memory job registry for the side-panel surface.

The CLI / Telegram / curl paths run a task SYNCHRONOUSLY (the caller blocks, and
approvals happen out-of-band on the phone). The side panel instead wants to:
  1. fire a task and get a job id back immediately,
  2. watch it progress live (steps), and
  3. approve/deny state-changing actions INLINE in the panel.

A `Job` holds an append-only event log (polled by GET /events?cursor=) plus a
set of pending approvals, each backed by a threading.Event that the agent's
approver blocks on until POST /approve resolves it (or it times out → deny).

All in-memory + best-effort — a process restart drops jobs (the kill-switch /
reaper own durability). Bounded so a long-lived surface can't leak memory.
"""
import asyncio
import json
import threading
import time

_MAX_EVENTS_PER_JOB = 500
_MAX_JOBS = 24


def _short(params, n: int = 240) -> str:
    try:
        return json.dumps(params, ensure_ascii=False)[:n]
    except Exception:
        return str(params)[:n]


class Job:
    def __init__(self, jid: str, task: str):
        self.id = jid
        self.task = task
        self.status = "running"      # running | done | error
        self.result = None           # the final run record
        self.created = time.time()
        self._events: list[dict] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._approvals: dict[str, dict] = {}   # aid -> {"event": Event, "decision": bool|None}
        self._aseq = 0

    # --- event log -------------------------------------------------------
    def add_event(self, etype: str, **data) -> dict:
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "t": round(time.time(), 3), "type": etype, **data}
            self._events.append(ev)
            if len(self._events) > _MAX_EVENTS_PER_JOB:
                self._events = self._events[-_MAX_EVENTS_PER_JOB:]
            return ev

    def since(self, cursor: int) -> dict:
        with self._lock:
            evs = [e for e in self._events if e["seq"] > cursor]
            return {"ok": True, "events": evs, "cursor": self._seq,
                    "status": self.status, "result": self.result}

    # --- step hook (best-effort live progress) ---------------------------
    def on_step(self, agent) -> None:
        n = None
        try:
            n = agent.state.n_steps
        except Exception:
            pass
        desc = None
        try:
            actions = agent.history.model_actions()
            if actions:
                desc = _short(actions[-1], 180)
        except Exception:
            desc = None
        url = None
        try:
            url = agent.history.urls()[-1]
        except Exception:
            url = None
        self.add_event("step", n=n, action=desc, url=url)

    # --- inline approval channel -----------------------------------------
    def make_approver(self, timeout_s: float = 240.0):
        """An async approve(name, params, page) that surfaces an approval event to
        the panel and blocks until POST /approve resolves it (timeout → deny)."""
        async def approve(name, params, page=None) -> bool:
            with self._lock:
                self._aseq += 1
                aid = f"a{self._aseq}"
            ev = threading.Event()
            self._approvals[aid] = {"event": ev, "decision": None}
            self.add_event("approval", id=aid, action=name, params=_short(params),
                           page=page, timeout_s=timeout_s)
            loop = asyncio.get_running_loop()

            def _wait():
                got = ev.wait(timeout_s)
                return (self._approvals.get(aid, {}).get("decision") is True) if got else False

            decision = await loop.run_in_executor(None, _wait)
            self.add_event("decision", id=aid, allow=bool(decision))
            return bool(decision)

        return approve

    def resolve(self, aid: str, decision: bool) -> bool:
        a = self._approvals.get(aid)
        if not a:
            return False
        a["decision"] = bool(decision)
        a["event"].set()
        return True


_REG: dict[str, Job] = {}
_REG_LOCK = threading.Lock()
_COUNTER = [0]


def new_job(task: str) -> Job:
    with _REG_LOCK:
        _COUNTER[0] += 1
        jid = f"j{int(time.time())}_{_COUNTER[0]}"
        # prune finished jobs if we're at the cap
        if len(_REG) >= _MAX_JOBS:
            finished = sorted((j for j in _REG.values() if j.status != "running"),
                              key=lambda j: j.created)
            for j in finished[: max(1, len(_REG) - _MAX_JOBS + 1)]:
                _REG.pop(j.id, None)
        job = Job(jid, task)
        _REG[jid] = job
        return job


def get_job(jid: str) -> Job | None:
    return _REG.get(jid)
