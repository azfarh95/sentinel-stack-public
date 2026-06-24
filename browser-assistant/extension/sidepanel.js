// Sentinel Assistant — portable panel. Runs as the Chrome side panel, as a web
// page served from the :8108 surface (GET /app), or in a WebView (e.g. Volery).
// Surface-agnostic: API base + token are auto-derived; chrome.* is feature-detected.
//
// Modes: Browse (delegate a goal to the gated browser agent — /run + /events +
// inline /approve) and Shop (fast price-sorted product search — /shop).

// ── portability: where's the backend, what's the token, how do we persist ──
function resolveBase() {
  if (typeof window !== "undefined" && window.SENTINEL_API_BASE) return window.SENTINEL_API_BASE;
  // served as a web page (http/https) → same origin; extension (chrome-extension://) → loopback
  if (location.protocol === "http:" || location.protocol === "https:") return location.origin;
  return "http://127.0.0.1:8108";
}
function resolveToken() {
  let t = (typeof window !== "undefined" && window.COMET_BRIDGE_TOKEN) || "";
  try {
    const u = new URL(location.href);
    const qp = u.searchParams.get("token");
    if (qp) {
      t = qp;
      try { localStorage.setItem("sentinel_token", qp); } catch {}
      u.searchParams.delete("token");              // don't leave the token in the address bar
      history.replaceState(null, "", u.toString());
    }
  } catch {}
  if (!t) { try { t = localStorage.getItem("sentinel_token") || ""; } catch {} }
  return t;
}
const BASE = resolveBase();
let TOKEN = resolveToken();

const store = {
  async get(k) {
    if (globalThis.chrome?.storage?.session) { try { return (await chrome.storage.session.get(k))[k]; } catch {} }
    try { return sessionStorage.getItem(k); } catch { return null; }
  },
  async set(k, v) {
    if (globalThis.chrome?.storage?.session) { try { await chrome.storage.session.set({ [k]: v }); return; } catch {} }
    try { sessionStorage.setItem(k, v); } catch {}
  },
  async del(k) {
    if (globalThis.chrome?.storage?.session) { try { await chrome.storage.session.remove(k); return; } catch {} }
    try { sessionStorage.removeItem(k); } catch {}
  },
};
function headers() {
  return { "Content-Type": "application/json", ...(TOKEN ? { "X-Comet-Token": TOKEN } : {}) };
}

// ── DOM ──────────────────────────────────────────────────────────────────
const $log = document.getElementById("ba-log");
const $empty = document.getElementById("ba-empty");
const $emptyLead = document.getElementById("ba-empty-lead");
const $examples = document.getElementById("ba-examples");
const $task = document.getElementById("ba-task");
const $run = document.getElementById("ba-run");
const $target = document.getElementById("ba-target");
const $vision = document.getElementById("ba-vision");
const $browseOpts = document.getElementById("ba-browse-opts");
const $status = document.getElementById("ba-status");
const $statusText = document.getElementById("ba-status-text");
const $meta = document.getElementById("ba-meta");
const $tabs = document.getElementById("ba-tabs");

let mode = "browse";   // browse | shop
let jobId = null, cursor = 0, polling = false;
const cards = {};
const POLL_MS = 1000;

const EXAMPLES = {
  browse: ["go to news.ycombinator.com and list the top 3 story titles",
           "go to example.com and report the H1 heading",
           "search wikipedia for 'rx 7900 xtx' and list the first 3 results"],
  shop: ["16gb ddr4 ram", "logitech mouse", "ssd 1tb nvme"],
};

// ── helpers ────────────────────────────────────────────────────────────────
function setStatus(state, text) {
  $status.classList.remove("ok", "error", "busy");
  if (state) $status.classList.add(state);
  $statusText.textContent = text;
}
function hideEmpty() { if ($empty && $empty.parentElement) $empty.remove(); }
function el(cls, text) { const d = document.createElement("div"); d.className = cls; if (text != null) d.textContent = text; return d; }
function append(node) { hideEmpty(); $log.appendChild(node); $log.scrollTop = $log.scrollHeight; return node; }
function showError(msg) { const r = append(el("ba-result err")); r.appendChild(el("span lbl", "error")); r.appendChild(document.createTextNode(msg)); }
function setRunning(on) {
  $run.disabled = on;
  $run.textContent = on ? "…" : (mode === "shop" ? "Search" : "Run");
  setStatus(on ? "busy" : "ok", on ? (mode === "shop" ? "searching…" : "running") : "ready");
}

function applyMode(m) {
  mode = m;
  [...$tabs.children].forEach(b => b.classList.toggle("active", b.dataset.mode === m));
  $browseOpts.style.display = (m === "browse") ? "" : "none";
  $task.placeholder = (m === "shop")
    ? "Search marketplaces…  e.g. 16gb ddr4 ram"
    : "Describe a task…  (Shift+Enter for newline, Enter to run)";
  $run.textContent = (m === "shop") ? "Search" : "Run";
  if ($emptyLead) $emptyLead.textContent = (m === "shop")
    ? "Search Shopee, Lazada, Amazon SG, Challenger & more for the cheapest match."
    : "Give the agent a task. It runs in a local browser and asks before it clicks or types.";
  if ($examples) {
    $examples.innerHTML = "";
    (EXAMPLES[m] || []).forEach(t => { const li = document.createElement("li"); li.textContent = t; $examples.appendChild(li); });
  }
}

// ── BROWSE: delegate a goal to the gated agent ──────────────────────────────
function handleEvent(ev) {
  switch (ev.type) {
    case "started":
      append(el("ba-task-line", ev.task));
      $meta.textContent = `${ev.mode}${ev.vision ? " · vision" : ""} · wall ${Math.round(ev.wall)}s`;
      break;
    case "step": {
      const s = el("ba-step");
      s.appendChild(el("b", `#${ev.n ?? "?"}`));
      s.appendChild(el("span", ev.action ? String(ev.action) : "thinking…"));
      if (ev.url) s.appendChild(el("span url", ev.url));
      append(s);
      break;
    }
    case "approval": append(renderApproval(ev)); break;
    case "decision": markResolved(ev.id, ev.allow); break;
    case "done": {
      const r = el("ba-result");
      r.appendChild(el("span lbl", `done · ${ev.status} · ${ev.steps ?? "?"} steps · ${ev.dur_s ?? "?"}s`));
      r.appendChild(document.createTextNode(ev.final || ev.err || "(no result)"));
      if (ev.status !== "ok") r.classList.add("err");
      append(r);
      break;
    }
    case "error": showError(ev.detail || "unknown error"); break;
  }
}
function renderApproval(ev) {
  const card = el("ba-approve"); card.dataset.id = ev.id;
  card.appendChild(el("hd", `Approve: ${ev.action}${ev.page ? "  ·  " + ev.page : ""}`));
  const det = el("det"); const code = document.createElement("code"); code.textContent = ev.params || ""; det.appendChild(code); card.appendChild(det);
  const btns = el("btns");
  const yes = document.createElement("button"); yes.className = "yes"; yes.textContent = "✓ Approve"; yes.onclick = () => decide(ev.id, true);
  const no = document.createElement("button"); no.className = "no"; no.textContent = "✗ Deny"; no.onclick = () => decide(ev.id, false);
  btns.appendChild(yes); btns.appendChild(no); card.appendChild(btns);
  cards[ev.id] = card; return card;
}
function markResolved(id, allow) {
  const card = cards[id];
  if (!card || card.classList.contains("resolved")) return;
  card.classList.add("resolved");
  card.appendChild(el(`verdict ${allow ? "yes" : "no"}`, allow ? "✓ approved" : "✗ denied"));
}
async function decide(id, allow) {
  const card = cards[id];
  if (card) card.querySelectorAll("button").forEach(b => (b.disabled = true));
  try { await fetch(`${BASE}/approve`, { method: "POST", headers: headers(), body: JSON.stringify({ job: jobId, id, decision: allow }) }); } catch {}
}
async function poll() {
  if (!jobId || polling) return;
  polling = true;
  try {
    const r = await fetch(`${BASE}/events?job=${encodeURIComponent(jobId)}&cursor=${cursor}`, { headers: headers() });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    (j.events || []).forEach(handleEvent);
    cursor = j.cursor ?? cursor;
    if (j.status === "running") { setTimeout(poll, POLL_MS); }
    else { setRunning(false); jobId = null; cursor = 0; await store.del("activeJob"); }
  } catch (e) { setStatus("error", "lost the surface — retrying"); setTimeout(poll, 2000); }
  finally { polling = false; }
}
async function runTask(task) {
  setRunning(true);
  try {
    const r = await fetch(`${BASE}/run`, { method: "POST", headers: headers(),
      body: JSON.stringify({ task, mode: $target.value, vision: $vision.checked, channel: "panel" }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) { showError(j.error ? `${j.error}${j.detail ? " — " + j.detail : ""}` : `HTTP ${r.status}`); setRunning(false); return; }
    jobId = j.job_id; cursor = 0; await store.set("activeJob", jobId); poll();
  } catch (e) { showError(`network: ${e.message} (is the surface up?)`); setRunning(false); }
}

// ── SHOP: fast product search → cards ───────────────────────────────────────
function renderListings(j) {
  const head = el("ba-shop-head", `${j.count ?? 0} listings · ${(j.sources_queried || []).join(", ") || "no sources"}`);
  append(head);
  (j.listings || []).slice(0, 20).forEach(l => {
    const c = el("ba-card");
    if (l.image_url) { const img = document.createElement("img"); img.src = l.image_url; img.loading = "lazy"; img.onerror = () => img.remove(); c.appendChild(img); }
    const body = el("body");
    body.appendChild(el("span price", l.price_sgd != null ? `S$${l.price_sgd}` : "price?"));
    body.appendChild(el("span ttl", l.title || "?"));
    const meta = el("src", `${l.marketplace || "?"}`);
    if (l.url) { const a = document.createElement("a"); a.href = l.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = " open ↗"; meta.appendChild(a); }
    body.appendChild(meta);
    c.appendChild(body);
    append(c);
  });
  const issues = j.issues || [];
  if (issues.length) append(el("ba-issues", "Some sources blocked/failed (not exhaustive): " + JSON.stringify(issues).slice(0, 160)));
}
async function runShop(query) {
  setRunning(true);
  append(el("ba-task-line", "🛒 " + query));
  try {
    const r = await fetch(`${BASE}/shop`, { method: "POST", headers: headers(), body: JSON.stringify({ query }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) { showError(j.error || `HTTP ${r.status}`); }
    else renderListings(j);
  } catch (e) { showError(`network: ${e.message}`); }
  finally { setRunning(false); }
}

// ── submit dispatch ─────────────────────────────────────────────────────────
function submit() {
  const text = $task.value.trim();
  if (!text || jobId) return;
  if (!TOKEN) { promptForToken(); return; }
  $task.value = "";
  if (mode === "shop") runShop(text); else runTask(text);
}

function promptForToken() {
  const box = append(el("ba-token-prompt"));
  box.appendChild(el("div", "A token is required. Paste COMET_BRIDGE_TOKEN (saved locally):"));
  const inp = document.createElement("input"); inp.type = "password"; inp.placeholder = "X-Comet-Token";
  inp.onkeydown = (e) => { if (e.key === "Enter") { TOKEN = inp.value.trim(); try { localStorage.setItem("sentinel_token", TOKEN); } catch {}; box.remove(); probeHealth(); } };
  box.appendChild(inp); inp.focus();
}

// ── health ──────────────────────────────────────────────────────────────────
async function probeHealth() {
  try {
    const r = await fetch(`${BASE}/health`, { cache: "no-store" });
    const j = await r.json();
    if (!j.enabled) { setStatus("error", "disabled (kill-switch)"); return; }
    if (jobId) return;
    setStatus(j.busy ? "busy" : "ok", j.busy ? "busy (another task)" : `ready · up ${j.uptime_s}s`);
  } catch { if (!jobId) setStatus("error", "surface offline"); }
}

// ── wire up ──────────────────────────────────────────────────────────────────
$run.addEventListener("click", submit);
$task.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
$tabs.addEventListener("click", (e) => { const b = e.target.closest(".ba-tab"); if (b) applyMode(b.dataset.mode); });
document.addEventListener("click", (e) => { const li = e.target.closest("#ba-examples li"); if (li) { $task.value = li.textContent.trim(); $task.focus(); } });

(async function init() {
  applyMode("browse");
  setStatus(null, "connecting…");
  // resume a browse job that was still running when the panel closed
  try { const aj = await store.get("activeJob"); if (aj) { jobId = aj; cursor = 0; setRunning(true); poll(); } } catch {}
  await probeHealth();
  setInterval(probeHealth, 15000);
})();
