// OpenClaw sidepanel — chat UI talking to bridge.py on 127.0.0.1:8101.
// Session id is per-Chromium-window so each window keeps its own thread.

const BRIDGE_BASE = "http://127.0.0.1:8101";
const HEALTH_URL  = `${BRIDGE_BASE}/health`;
const CHAT_URL    = `${BRIDGE_BASE}/chat`;
// Shared bridge token (closes the S1 hole). Set by config.local.js (gitignored);
// "" if not provisioned — the bridge then runs fail-open.
const BRIDGE_TOKEN = (typeof window !== "undefined" && window.COMET_BRIDGE_TOKEN) || "";
const REQUEST_TIMEOUT_MS = 10 * 60 * 1000; // 10 min — observed tool-using turns up to 4.6 min

const $log         = document.getElementById("oc-log");
const $empty       = document.getElementById("oc-empty");
const $msg         = document.getElementById("oc-msg");
const $send        = document.getElementById("oc-send");
const $status      = document.getElementById("oc-status");
const $statusText  = document.getElementById("oc-status-text");
const $meta        = document.getElementById("oc-meta");
const $ctx         = document.getElementById("oc-ctx");
const $ctxText     = document.getElementById("oc-ctx-text");
const $ctxFill     = document.getElementById("oc-ctx-fill");
const $newBtn      = document.getElementById("oc-new");

let sessionId = "browser-default";
let pending = false;

// ── helpers ────────────────────────────────────────────────────────────────
function setStatus(state, text, modelLine) {
  $status.classList.remove("ok", "error");
  if (state) $status.classList.add(state);
  $statusText.textContent = text;
  if (modelLine !== undefined) $meta.textContent = modelLine || "";
}

function hideEmpty() {
  if ($empty && $empty.parentElement) $empty.remove();
}

function addMessage(role, text, meta) {
  hideEmpty();
  const div = document.createElement("div");
  div.className = `oc-msg ${role}`;
  div.textContent = text;
  if (meta) {
    const m = document.createElement("span");
    m.className = "oc-msg-meta";
    m.textContent = meta;
    div.appendChild(m);
  }
  $log.appendChild(div);
  $log.scrollTop = $log.scrollHeight;
  return div;
}

function addTyping() {
  hideEmpty();
  const div = document.createElement("div");
  div.className = "oc-typing";
  const t0 = performance.now();
  const tick = () => {
    const s = Math.round((performance.now() - t0) / 1000);
    div.textContent = `OpenClaw is thinking · ${s}s`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  $log.appendChild(div);
  $log.scrollTop = $log.scrollHeight;
  return {
    el: div,
    remove() { clearInterval(timer); div.remove(); },
  };
}

function timeFmt(ms) {
  if (!ms && ms !== 0) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function updateContext(used, limit) {
  if (!used || !limit) { $ctx.hidden = true; return; }
  $ctx.hidden = false;
  const pct = Math.min(100, Math.round((used / limit) * 100));
  $ctxFill.style.width = `${pct}%`;
  $ctxText.textContent = `${used.toLocaleString()} / ${limit.toLocaleString()} tokens · ${pct}%`;
  $ctx.classList.toggle("warn", pct >= 75 && pct < 95);
  $ctx.classList.toggle("crit", pct >= 95);
}

async function deriveSessionId() {
  // Honour an explicit override from storage (set by "New" button / /new command).
  const stored = await chrome.storage.session.get("sessionId");
  if (stored.sessionId) { sessionId = stored.sessionId; return; }
  try {
    const win = await chrome.windows.getCurrent();
    sessionId = `browser-win-${win.id}`;
  } catch {
    sessionId = `browser-${Date.now()}`;
  }
  await chrome.storage.session.set({ sessionId });
}

async function startNewSession() {
  // Generate a fresh session-id (timestamp suffix so multiple resets within the
  // same window each get their own). Bridge regex allows [A-Za-z0-9._-]{1,64}.
  let base = "browser";
  try {
    const win = await chrome.windows.getCurrent();
    base = `browser-win-${win.id}`;
  } catch {}
  sessionId = `${base}-${Date.now()}`;
  await chrome.storage.session.set({ sessionId });
  // Clear log + reset counter
  $log.innerHTML = "";
  const empty = document.createElement("div");
  empty.id = "oc-empty";
  empty.className = "oc-empty";
  empty.innerHTML = `<p>Fresh session — context cleared.</p><p style="font-size:11px;margin-top:4px;">session: <code>${sessionId}</code></p>`;
  $log.appendChild(empty);
  updateContext(0, 0);  // hides the bar
  setStatus("ok", `ready · new session`);
  $msg.focus();
}

// ── health polling ──────────────────────────────────────────────────────────
async function probeHealth() {
  try {
    const r = await fetch(HEALTH_URL, { method: "GET", cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    setStatus("ok", `bridge up · ${j.uptime_s}s`);
    return true;
  } catch (e) {
    setStatus("error", "bridge offline");
    return false;
  }
}

// ── send loop ──────────────────────────────────────────────────────────────
async function sendMessage(text) {
  if (pending || !text.trim()) return;
  pending = true;
  $send.disabled = true;
  $msg.disabled = true;

  addMessage("user", text);
  const typing = addTyping();

  const t0 = performance.now();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);

  try {
    const resp = await fetch(CHAT_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(BRIDGE_TOKEN ? { "X-Comet-Token": BRIDGE_TOKEN } : {}),
      },
      body: JSON.stringify({ message: text, session_id: sessionId }),
      signal: ctl.signal,
    });
    clearTimeout(timer);
    const body = await resp.json().catch(() => ({}));
    typing.remove();

    if (!resp.ok || !body.ok) {
      const err = body.error || `HTTP ${resp.status}`;
      const detail = body.detail ? ` — ${String(body.detail).slice(0, 240)}` : "";
      addMessage("error", `${err}${detail}`);
      setStatus("error", "agent error");
      return;
    }
    const elapsedMs = Math.round(performance.now() - t0);
    const replyMeta = [
      body.model || "qwen",
      body.duration_ms ? timeFmt(body.duration_ms) : timeFmt(elapsedMs),
      body.usage ? `${body.usage.input}→${body.usage.output} tok` : null,
    ].filter(Boolean).join(" · ");
    addMessage("assistant", body.reply || "(empty reply)", replyMeta);
    setStatus("ok", "ready", `last turn: ${replyMeta}`);
    if (body.usage && body.context_limit) {
      updateContext(body.usage.input, body.context_limit);
    }
  } catch (e) {
    typing.remove();
    if (e.name === "AbortError") {
      addMessage("error", "timed out after 10 min");
    } else {
      addMessage("error", `network: ${e.message}`);
    }
    setStatus("error", "transport error");
  } finally {
    clearTimeout(timer);
    pending = false;
    $send.disabled = false;
    $msg.disabled = false;
    $msg.focus();
  }
}

// ── wire up ────────────────────────────────────────────────────────────────
$send.addEventListener("click", () => {
  const text = $msg.value.trim();
  if (!text) return;
  $msg.value = "";
  // Client-side slash-commands. /new resets the session; /reset is an alias.
  if (text === "/new" || text === "/reset") {
    startNewSession();
    return;
  }
  sendMessage(text);
});

$newBtn.addEventListener("click", () => {
  if (pending) return;
  startNewSession();
});

$msg.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $send.click();
  }
});

// example chips
if ($empty) {
  $empty.addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    $msg.value = li.textContent.replace(/^[“"]|[”"]$/g, "").trim();
    $msg.focus();
  });
}

(async function init() {
  setStatus(null, "connecting…");
  await deriveSessionId();
  const up = await probeHealth();
  if (up) setStatus("ok", `ready · ${sessionId}`);
  setInterval(probeHealth, 30_000);
})();
