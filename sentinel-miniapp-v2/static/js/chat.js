// Sentinel shared-brain chat — Wave 1 (Phase 8.0).
// Three-pane modern LLM-UI layout: threads | messages | tools.
// Vanilla, single-file, no build step.

(() => {
  const _SESSION_KEY = 'sentinel_v2_session';
  const _sentinelToken = window.SENTINEL_TOKEN || '';
  const CONTEXT_LIMIT = 131072;

  const tg = window.Telegram?.WebApp;
  tg?.ready?.();
  tg?.expand?.();

  // ── Session ─────────────────────────────────────────────────────────
  function _getSession() {
    try {
      const s = JSON.parse(localStorage.getItem(_SESSION_KEY) || 'null');
      if (s && s.token && s.expires_at * 1000 > Date.now()) return s;
    } catch {}
    return null;
  }
  function _adoptApkSession() {
    try {
      const t = window.APK_SESSION_TOKEN;
      if (!t) return false;
      const existing = _getSession();
      if (existing && existing.token === t) return true;
      localStorage.setItem(_SESSION_KEY, JSON.stringify({
        token: t, expires_at: Math.floor(Date.now() / 1000) + 8 * 3600,
      }));
      return true;
    } catch { return false; }
  }
  async function api(path, opts = {}) {
    const session = _getSession();
    if (!session) throw new Error('auth_required');
    const res = await fetch(path, {
      ...opts,
      headers: {
        'Content-Type': 'application/json',
        'X-Sentinel-Token': _sentinelToken,
        'X-Session-Token': session.token,
        ...(opts.headers || {}),
      },
    });
    if (res.status === 401 || res.status === 403) throw new Error('auth_required');
    if (!res.ok) {
      const t = await res.text();
      // Gateway/proxy errors (Cloudflare 524, nginx 502/504) return a full
      // HTML error page — don't dump raw HTML into an alert(). Collapse it to
      // a concise message and flag it as a gateway/timeout class error so
      // callers can treat it as "still in flight" rather than a hard failure.
      const looksHtml = /^\s*<(?:!doctype|html)/i.test(t);
      const isGateway = looksHtml || [502, 503, 504, 524].includes(res.status);
      const err = new Error(isGateway ? `${res.status} (gateway timeout)` : `${res.status}: ${t.slice(0, 200)}`);
      err.status = res.status;
      err.gateway = isGateway;
      throw err;
    }
    return res.json();
  }

  // ── State ───────────────────────────────────────────────────────────
  const state = {
    threads: [],
    activeId: null,
    messages: [],
    nextSince: 0,
    sending: false,
    inventory: { servers: [], skills: [] },
    pinnedTool: '',
    contextTokens: 0,
    ws: null,
    wsBackoff: 1000,
    wsSubs: new Set(),
    awaitingReply: null,   // {thread, asstId} while an async turn is in flight
    _awaitTimer: null,
  };

  // ── DOM ────────────────────────────────────────────────────────────
  const $ = (s) => document.querySelector(s);
  const els = {
    threadList:  $('#thread-list'),
    settingsLink:$('#settings-link'),
    settingsShortcut:$('#settings-shortcut'),
    threadTitle:$('#thread-title'),
    refreshBtn: $('#refresh-btn'),
    renameBtn:  $('#rename-btn'),
    archiveBtn: $('#archive-btn'),
    msgList:    $('#msg-list'),
    jumpLatest: $('#jump-latest'),
    typing:     $('#typing'),
    composer:   $('#composer'),
    composerText:$('#composer-text'),
    newThreadSide:$('#new-thread-side'),
    attachBtn:  $('#attach-btn'),
    fileInput:  $('#file-input'),
    attachments:$('#attachments'),
    dropOverlay:$('#drop-overlay'),
    chatMain:   $('#chat-main'),
    toolPin:    $('#tool-pin'),
    sendBtn:    $('#send-btn'),
    ctxCounter: $('#ctx-counter'),
    resetBtn:   $('#reset-btn'),
    integrationsList: $('#integrations-list'),
    settingsModal:$('#settings-modal'),
    settingsClose:$('#settings-close'),
    settingsMcp:$('#settings-mcp'),
    settingsSkills:$('#settings-skills'),
    authReq:    $('#auth-required'),
    chatApp:    $('#chat-app'),
    threadsAside:$('#threads'),
    toolsAside: $('#tools-bar'),
    toggleThreads:$('#toggle-threads'),
    toggleTools:$('#toggle-tools'),
  };

  // ── Helpers ────────────────────────────────────────────────────────
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  // Whitespace-only (or empty) content. Used to collapse blank assistant/
  // tool/system turns into a small chip instead of an empty bubble that
  // leaves a big dead gap in the message list.
  function isBlank(s) {
    return !s || !String(s).replace(/[\s ​]+/g, '').length;
  }
  function fmtTokens(n) {
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(n);
  }
  function showAuthRequired() {
    els.chatApp.style.display = 'none';
    els.authReq.style.display = 'block';
  }

  // ── Render: threads ────────────────────────────────────────────────
  function renderThreads() {
    if (!state.threads.length) {
      els.threadList.innerHTML = '<div class="empty">No threads yet.</div>';
      return;
    }
    els.threadList.innerHTML = '';
    for (const t of state.threads) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'thread-row' + (t.id === state.activeId ? ' active' : '');
      row.dataset.threadId = t.id;
      row.innerHTML = `
        <span class="thread-name">${esc(t.name)}</span>
        <span class="thread-count">${t.message_count || 0}</span>
      `;
      row.addEventListener('click', () => switchTo(t.id));
      els.threadList.appendChild(row);
    }
  }

  // ── Render: messages ──────────────────────────────────────────────
  // Per-message fingerprint. When this changes the row is re-rendered;
  // when it's identical the existing DOM node is left untouched (so the
  // browser doesn't re-paint / re-highlight stable bubbles on every poll).
  function _msgSig(m) {
    return [
      m.id,
      m.streaming_done === false ? 'S' : 'D',
      (m.content || '').length,
      m.attachments ? m.attachments.length : 0,
      m.tokens_out || 0,
      m.model || '',
    ].join('|');
  }
  function _isNearBottom() {
    const el = els.msgList;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 140;
  }
  function _scrollToBottom() {
    els.msgList.scrollTop = els.msgList.scrollHeight;
    _hideJumpPill();
  }
  function _showJumpPill() { if (els.jumpLatest) els.jumpLatest.hidden = false; }
  function _hideJumpPill() { if (els.jumpLatest) els.jumpLatest.hidden = true; }

  // Incremental reconcile against the live DOM. `scroll`:
  //   'force' → always snap to bottom (user's own send / thread switch)
  //   true    → snap only if already near the bottom, else show the pill
  //   false   → never scroll
  function renderMessages(scroll = true) {
    const list = els.msgList;
    // An assistant row reserved by chat_turn_begin is empty + streaming until
    // the background turn finalises it. Don't render it as a "no text" chip —
    // the global #typing indicator is the in-flight signal; the bubble appears
    // once it has real content.
    const msgs = state.messages.filter(
      m => !(m.role !== 'user' && m.streaming_done === false && isBlank(m.content)),
    );
    if (!msgs.length) {
      list.innerHTML = '<div class="empty">No messages yet. Say hi.</div>';
      state._renderedSig = new Map();
      _hideJumpPill();
      return;
    }
    const wasNearBottom = _isNearBottom();
    const ph = list.querySelector('.empty');
    if (ph) ph.remove();

    const prevSig = state._renderedSig || new Map();
    const nextSig = new Map();
    const existing = new Map();
    for (const node of list.children) {
      if (node.dataset && node.dataset.msgId) existing.set(node.dataset.msgId, node);
    }
    const changedNodes = [];
    let prevNode = null;
    for (const m of msgs) {
      const id = String(m.id);
      const sig = _msgSig(m);
      nextSig.set(id, sig);
      let node = existing.get(id);
      if (!node) {
        node = messageRow(m);
        if (prevNode) prevNode.after(node); else list.prepend(node);
        changedNodes.push(node);
      } else if (prevSig.get(id) !== sig) {
        const fresh = messageRow(m);
        node.replaceWith(fresh);
        node = fresh;
        changedNodes.push(node);
      } else {
        const shouldFollow = prevNode ? prevNode.nextSibling : list.firstChild;
        if (node !== shouldFollow) {
          if (prevNode) prevNode.after(node); else list.prepend(node);
        }
      }
      existing.delete(id);
      prevNode = node;
    }
    // Drop nodes whose messages are gone (e.g. optimistic replaced by real rows)
    for (const [, node] of existing) node.remove();
    state._renderedSig = nextSig;

    for (const n of changedNodes) _decorateNode(n);

    if (scroll === 'force') _scrollToBottom();
    else if (scroll && wasNearBottom) _scrollToBottom();
    else if (changedNodes.length && !wasNearBottom) _showJumpPill();
  }
  // Syntax-highlight any fenced code in a freshly rendered node. No-op when
  // Prism hasn't loaded (CDN blocked) — code still shows as plain <pre>.
  function _decorateNode(node) {
    if (!window.Prism || !node.querySelectorAll) return;
    node.querySelectorAll('pre code[class*="language-"]').forEach(c => {
      try { window.Prism.highlightElement(c); } catch {}
    });
  }
  function _highlightAll() {
    if (!window.Prism) return;
    els.msgList.querySelectorAll('pre code[class*="language-"]').forEach(c => {
      try { window.Prism.highlightElement(c); } catch {}
    });
  }
  async function _copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      try {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      } catch { return; }
    }
    const old = btn.textContent;
    btn.classList.add('copied');
    btn.textContent = btn.classList.contains('msg-copy') ? '✓' : '✓ Copied';
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = old; }, 1200);
  }
  function messageRow(m) {
    const div = document.createElement('div');
    div.dataset.msgId = m.id;
    // Blank non-user turn (e.g. tool-only reply) → collapse to a chip so it
    // doesn't render as an empty bubble + dead vertical space.
    if (m.role !== 'user' && isBlank(m.content)) {
      div.className = `msg ${m.role} msg-collapsed`;
      div.innerHTML = `<span class="dot"></span><span>no text returned</span>`;
      return div;
    }
    div.className = `msg ${m.role}`;
    const surface = m.surface ? `<span class="surface-tag">${esc(m.surface)}</span>` : '';
    const model = m.model ? ` · ${esc(m.model)}` : '';
    const tokOut = m.tokens_out ? ` · ${m.tokens_out}t` : '';
    const summary = m.is_summary ? ` <span class="summary-tag">summary</span>` : '';
    const ts = m.created_at ? new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    // User-typed text renders literally (no markdown — what they typed is what they meant).
    // Everything else (assistant / tool / system / summary) gets the markdown pass.
    const bodyHtml = (m.role === 'user')
      ? esc(m.content || '')
      : renderMarkdown(m.content || '');
    const attachHtml = (m.attachments && m.attachments.length)
      ? `<div class="msg-attachments">` + m.attachments.map(a =>
          `<span class="msg-attach-chip" title="${esc(a.name)}">📎 <span class="nm">${esc(a.name)}</span></span>`
        ).join('') + `</div>`
      : '';
    // Copy affordance on assistant replies (copies the raw markdown). User
    // bubbles are short and self-evidently theirs, so skip the clutter there.
    const copyBtn = (m.role === 'assistant' && !isBlank(m.content))
      ? `<button type="button" class="msg-copy" title="Copy reply">⧉</button>`
      : '';
    div.innerHTML = `
      ${copyBtn}<div class="msg-body">${bodyHtml}</div>${attachHtml}
      <div class="msg-meta">${surface}${ts ? '<span>' + ts + '</span>' : ''}${model ? '<span>' + model + '</span>' : ''}${tokOut ? '<span>' + tokOut + '</span>' : ''}${summary}</div>
    `;
    return div;
  }

  // OpenClaw's system prompt + ~80 tool defs + fresh-session setup is
  // roughly constant per turn. Empirical floor observed in clean turns:
  //   - Pre-Phase-7.5 (session-id reused): ~45-50K
  //   - Post-Phase-7.5 (fresh session per turn): ~65K
  // The +15K is OpenClaw re-establishing context every call instead of
  // reusing the prior session's cache — a deliberate trade for eliminating
  // the bridge_error bug class. Disable MCP tool servers in the right
  // sidebar to drop this floor (~300 tokens per tool def).
  const OPENCLAW_BASE_TOKENS = 65_000;

  function updateContextCounter() {
    // Counter shows YOUR usage against the effective budget — i.e. what's
    // left after OpenClaw's fixed ~65k base. This matches the intuition of
    // "fuel gauge starts at 0, grows with use" rather than "starts at 65k
    // because the engine idles". The absolute (preamble + base) / total
    // available on hover.
    let preambleTokens = 0;
    let actualLast = null;
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const m = state.messages[i];
      if (m.streaming_done === false) continue;
      if (actualLast === null && m.role === 'assistant' && m.tokens_in) {
        actualLast = m.tokens_in;
      }
      const content = m.content || '';
      preambleTokens += Math.max(1, Math.floor(content.length / 4));
      if (m.is_summary) break;
    }
    const effectiveBudget = CONTEXT_LIMIT - OPENCLAW_BASE_TOKENS; // ~66k
    state.contextTokens = OPENCLAW_BASE_TOKENS + preambleTokens;
    const pct = Math.min(1, preambleTokens / effectiveBudget);
    const cls = pct > 0.85 ? 'danger' : pct > 0.6 ? 'warn' : '';
    const absolute = OPENCLAW_BASE_TOKENS + preambleTokens;
    const tooltip = (
      `Thread usage: ${fmtTokens(preambleTokens)} of ${fmtTokens(effectiveBudget)} available.\n` +
      `Absolute: ${fmtTokens(absolute)} / ${fmtTokens(CONTEXT_LIMIT)} (incl. ${fmtTokens(OPENCLAW_BASE_TOKENS)} OpenClaw base — system + ~80 tool defs).\n` +
      `Disable MCP servers you don't need to drop the base.`
    );
    let html = `<span class="${cls}" title="${tooltip}">${fmtTokens(preambleTokens)}</span> / ${fmtTokens(effectiveBudget)}`;
    // Show actual-last only when LM reports something materially different
    // (e.g. mid-turn tool_result inflation, or pre-Reset bloat lingering).
    if (actualLast !== null) {
      const actualThreadPart = Math.max(0, actualLast - OPENCLAW_BASE_TOKENS);
      if (Math.abs(actualThreadPart - preambleTokens) > Math.max(1500, preambleTokens * 0.3)) {
        const actCls = actualLast / CONTEXT_LIMIT > 0.85 ? 'danger' : actualLast / CONTEXT_LIMIT > 0.6 ? 'warn' : '';
        html += ` <span class="ctx-actual" title="Actual tokens used on the last turn (incl. any tool_result blobs OpenClaw fetched mid-turn). The projection above only counts what's visible in this thread's message list.">· last <span class="${actCls}">${fmtTokens(actualThreadPart)}</span></span>`;
      }
    }
    els.ctxCounter.innerHTML = html;
  }

  // ── Render: integrations sidebar (LM Studio style) ────────────────
  // One row per service. Switch toggles ALL tools in that service.
  // Indeterminate = mixed (some on, some off).
  function renderToolsSummary() {
    const { servers, skills } = state.inventory;
    const liveServers = servers
      .filter(s => (s.tools_total || 0) > 0)
      .sort((a, b) => a.name.localeCompare(b.name));
    const emptyServers = servers
      .filter(s => (s.tools_total || 0) === 0)
      .sort((a, b) => a.name.localeCompare(b.name));

    const mcpTotal = liveServers.reduce((n, s) => n + (s.tools_total || 0), 0);
    const mcpOn = liveServers.reduce((n, s) => n + (s.tools_total || 0) - (s.tools_disabled || 0), 0);

    let html = '';
    html += `<div class="section-row"><span>MCP</span><span>${mcpOn}/${mcpTotal} tools</span></div>`;
    for (const s of liveServers) {
      const total = s.tools_total || 0;
      const on = total - (s.tools_disabled || 0);
      const allOff = on === 0;
      const allOn = on === total;
      html += `<div class="service-row ${allOff ? 'all-off' : ''}" data-server="${esc(s.name)}">
        <span class="name" title="${esc(s.name)}">${esc(s.name)}</span>
        <span class="cnt">${on}/${total}</span>
        <label class="switch">
          <input type="checkbox" data-server-toggle="${esc(s.name)}" ${allOn ? 'checked' : ''} />
          <span class="slider"></span>
        </label>
      </div>`;
    }
    if (emptyServers.length) {
      // Collapsed by default — these are servers MetaMCP has registered
      // but not yet enumerated tools for (lazy populate on first use).
      // Click the section header to reveal them.
      html += `<details class="section-details" style="margin-top:6px">
        <summary class="section-row" style="cursor:pointer">
          <span>Not yet active <span style="opacity:0.5">▸</span></span>
          <span>${emptyServers.length}</span>
        </summary>`;
      for (const s of emptyServers) {
        html += `<div class="service-row empty-server"><span class="name" title="${esc(s.name)}">${esc(s.name)}</span><span class="cnt">—</span><span></span></div>`;
      }
      html += `</details>`;
    }

    if (skills.length) {
      const skOn = skills.filter(sk => !sk.disabled).length;
      html += `<div class="section-row" style="margin-top:8px"><span>Skills</span><span>${skOn}/${skills.length}</span></div>`;
      for (const sk of skills) {
        html += `<div class="service-row ${sk.disabled ? 'all-off' : ''}">
          <span class="name">${esc(sk.name)}</span>
          <span class="cnt">${sk.disabled ? 'off' : 'on'}</span>
          <label class="switch">
            <input type="checkbox" disabled ${sk.disabled ? '' : 'checked'} />
            <span class="slider"></span>
          </label>
        </div>`;
      }
    }

    els.integrationsList.innerHTML = html || '<div class="empty">No integrations loaded.</div>';
    // Apply indeterminate state where needed (HTML can't express it)
    for (const s of liveServers) {
      const input = els.integrationsList.querySelector(`input[data-server-toggle="${cssEsc(s.name)}"]`);
      if (!input) continue;
      const total = s.tools_total || 0;
      const on = total - (s.tools_disabled || 0);
      input.indeterminate = on > 0 && on < total;
    }
    // Wire toggles
    els.integrationsList.querySelectorAll('input[data-server-toggle]').forEach(input => {
      input.addEventListener('change', (e) => {
        const name = e.target.dataset.serverToggle;
        toggleService(name, e.target.checked);
      });
    });
  }

  function cssEsc(s) {
    // CSS.escape isn't on every browser; pragmatic fallback for our names.
    return String(s).replace(/(["\\\]])/g, '\\$1');
  }

  async function toggleService(name, on) {
    const server = state.inventory.servers.find(s => s.name === name);
    if (!server || !state.activeId) return;
    const updates = [];
    for (const t of (server.tools || [])) {
      const want = on;
      const cur = t.status !== 'INACTIVE';
      if (want !== cur) {
        updates.push(api(`/api/brain/threads/${state.activeId}/tools/toggle`, {
          method: 'POST',
          body: JSON.stringify({
            tool_uuid: t.tool_uuid,
            server_uuid: t.mcp_server_uuid,
            enabled: want,
          }),
        }).then(() => { t.status = want ? 'ACTIVE' : 'INACTIVE'; t.enabled = want; })
          .catch(err => console.warn('toggle failed', err)));
      }
    }
    await Promise.allSettled(updates);
    server.tools_disabled = (server.tools || []).filter(x => !x.enabled).length;
    server.tools_enabled = (server.tools || []).filter(x => x.enabled).length;
    renderToolsSummary();
    renderToolPinDropdown();
    renderSettingsModal();
  }

  function renderToolPinDropdown() {
    // LM-Studio-style: one entry per SERVICE (MCP server / skill).
    // Per-tool granularity stays in Settings. Sort: services with tools
    // first (alphabetical), then skills.
    const opts = [`<option value="">No service pinned</option>`];
    const liveServers = state.inventory.servers
      .filter(s => (s.tools_total || 0) > 0)
      .sort((a, b) => a.name.localeCompare(b.name));
    for (const s of liveServers) {
      const on = (s.tools_total || 0) - (s.tools_disabled || 0);
      const dis = on === 0 ? ' (all off)' : '';
      opts.push(`<option value="mcp:${esc(s.name)}">${esc(s.name)}${dis}</option>`);
    }
    for (const sk of state.inventory.skills) {
      const dis = sk.disabled ? ' (off)' : '';
      opts.push(`<option value="skill:${esc(sk.id || sk.name)}">${esc(sk.name)} (skill)${dis}</option>`);
    }
    els.toolPin.innerHTML = opts.join('');
    els.toolPin.value = state.pinnedTool || '';
  }

  // ── Render: settings modal ────────────────────────────────────────
  function renderSettingsModal() {
    // MCP tools — collapsible per server, with switches for each tool.
    let mcpHtml = '';
    for (const s of state.inventory.servers) {
      const total = s.tools_total || 0;
      if (total === 0) continue;
      const on = total - (s.tools_disabled || 0);
      mcpHtml += `<details data-server="${esc(s.name)}">
        <summary>
          <span class="nm">${esc(s.name)}</span>
          <span class="right"><span>${on}/${total}</span></span>
        </summary>
        <div class="tools-of-server">
          ${(s.tools || []).map(t => `
            <div class="tool-row">
              <div class="meta">
                <div class="nm">${esc(t.tool_name)}</div>
                ${t.description ? `<div class="desc">${esc(t.description.slice(0, 200))}</div>` : ''}
              </div>
              <label class="switch">
                <input type="checkbox"
                       data-tool-uuid="${esc(t.tool_uuid)}"
                       data-server-uuid="${esc(t.mcp_server_uuid)}"
                       ${t.enabled !== false ? 'checked' : ''} />
                <span class="slider"></span>
              </label>
            </div>
          `).join('')}
        </div>
      </details>`;
    }
    els.settingsMcp.innerHTML = mcpHtml || '<div class="empty">No MCP tools loaded.</div>';
    // wire toggle handlers — per-thread now (writes to brain.thread_tool_overrides)
    els.settingsMcp.querySelectorAll('input[data-tool-uuid]').forEach(input => {
      input.addEventListener('change', async (e) => {
        const uuid = e.target.dataset.toolUuid;
        const serverUuid = e.target.dataset.serverUuid;
        const enabled = e.target.checked;
        if (!state.activeId) {
          alert('No active thread.');
          e.target.checked = !enabled;
          return;
        }
        try {
          await api(`/api/brain/threads/${state.activeId}/tools/toggle`, {
            method: 'POST',
            body: JSON.stringify({
              tool_uuid: uuid,
              server_uuid: serverUuid,
              enabled,
            }),
          });
          // Optimistic local update
          for (const s of state.inventory.servers) {
            for (const t of (s.tools || [])) {
              if (t.tool_uuid === uuid) {
                t.status = enabled ? 'ACTIVE' : 'INACTIVE';
                t.enabled = enabled;
              }
            }
            s.tools_enabled = (s.tools || []).filter(x => x.enabled).length;
            s.tools_disabled = (s.tools_total || 0) - s.tools_enabled;
          }
          renderToolsSummary();
          renderToolPinDropdown();
        } catch (err) {
          alert(`toggle failed: ${err.message}`);
          e.target.checked = !enabled;
        }
      });
    });

    // Skills
    let skillsHtml = '';
    for (const sk of state.inventory.skills) {
      skillsHtml += `<details>
        <summary>
          <span class="nm">${esc(sk.name)}</span>
          <span class="right"><span>${sk.disabled ? 'off' : 'on'}</span></span>
        </summary>
        <div class="tools-of-server">
          <div class="tool-row">
            <div class="meta">
              <div class="nm">${esc(sk.id || sk.name)}</div>
              <div class="desc">Wave-2 toggle wiring pending; skills currently read-only.</div>
            </div>
            <label class="switch">
              <input type="checkbox" disabled ${sk.disabled ? '' : 'checked'} />
              <span class="slider"></span>
            </label>
          </div>
        </div>
      </details>`;
    }
    els.settingsSkills.innerHTML = skillsHtml || '<div class="empty">No skills detected.</div>';
  }

  // ── Inventory load ─────────────────────────────────────────────────
  // Per-thread now: inventory reflects THIS thread's loadout. Switching
  // threads triggers a reload. Toggling writes to per-thread overrides.
  async function loadInventory() {
    if (!state.activeId) {
      els.integrationsList.innerHTML = '<div class="empty">Pick a thread first.</div>';
      return;
    }
    try {
      const r = await api(`/api/brain/threads/${state.activeId}/tools`);
      const servers = r.servers || [];
      // Decorate server-level status with override-aware counts (already
      // included as tools_enabled / tools_disabled by the endpoint)
      for (const s of servers) {
        for (const t of (s.tools || [])) {
          t.status = t.enabled ? 'ACTIVE' : 'INACTIVE';
        }
      }
      state.inventory.servers = servers;
      // Skills inventory — read-only (OpenClaw decides what to load at boot)
      try {
        const sk = await api('/api/brain/skills');
        state.inventory.skills = sk.skills || [];
      } catch (e) {
        console.warn('skills load failed', e);
        state.inventory.skills = [];
      }
      renderToolsSummary();
      renderToolPinDropdown();
      renderSettingsModal();
    } catch (e) {
      console.warn('inventory load failed', e);
      els.integrationsList.innerHTML = `<div class="empty">Inventory load failed: ${esc(e.message)}</div>`;
    }
  }

  // ── Threads + messages ────────────────────────────────────────────
  async function refreshThreads() {
    const data = await api('/api/brain/threads');
    state.threads = data.threads || [];
    if (!state.activeId) {
      state.activeId = data.active_thread_id || (state.threads[0]?.id ?? null);
    }
    renderThreads();
    const t = state.threads.find(x => x.id === state.activeId);
    els.threadTitle.textContent = t ? t.name : '—';
  }
  async function loadMessages() {
    if (!state.activeId) {
      state.messages = []; renderMessages(); updateContextCounter(); return;
    }
    const data = await api(`/api/brain/threads/${state.activeId}/messages?limit=500`);
    state.messages = data.messages || [];
    state.nextSince = data.next_since || 0;
    renderMessages('force');
    updateContextCounter();
  }
  async function pullTail() {
    if (!state.activeId || state.sending) return;
    try {
      const data = await api(`/api/brain/threads/${state.activeId}/messages?since=${state.nextSince}&limit=50`);
      const fresh = data.messages || [];
      if (fresh.length) {
        state.messages = state.messages.filter(m => typeof m.id === 'number');
        state.messages = state.messages.concat(fresh);
        state.nextSince = data.next_since || state.nextSince;
        renderMessages();
        updateContextCounter();
      }
    } catch (e) { console.warn('pullTail failed', e); }
  }
  async function switchTo(threadId) {
    if (threadId === state.activeId) return;
    state.activeId = threadId;
    state.messages = []; state.nextSince = 0;
    try {
      await api('/api/brain/active', { method: 'POST', body: JSON.stringify({ thread_id: threadId }) });
    } catch (e) { console.warn(e); }
    await refreshThreads();
    await loadMessages();
    _subscribeActive();
    // Per-thread tool loadout: reload integrations sidebar for the new thread
    loadInventory();
  }
  async function createThread(name) {
    if (!name) return;
    try {
      const t = await api('/api/brain/threads', {
        method: 'POST',
        body: JSON.stringify({ name, kind: 'general', switch: true }),
      });
      state.activeId = t.id;
      state.messages = []; state.nextSince = 0;
      await refreshThreads();
      await loadMessages();
      _subscribeActive();
    } catch (e) { alert(`create failed: ${e.message}`); }
  }
  async function archiveActive() {
    if (!state.activeId) return;
    if (!confirm(`Archive thread "${els.threadTitle.textContent}"?`)) return;
    const r = await api(`/api/brain/threads/${state.activeId}/archive`, { method: 'POST' });
    state.activeId = r.active_thread_id || null;
    state.messages = []; state.nextSince = 0;
    await refreshThreads();
    await loadMessages();
    _subscribeActive();
  }

  // Inline rename. Native prompt() is a silent no-op in many Android
  // WebViews/TWAs, so we swap the title <h2> for an <input> instead.
  async function renameActive() {
    if (!state.activeId) return;
    if (els.threadTitle.dataset.editing) return;
    const raw = els.threadTitle.textContent || '';
    const cur = raw === '—' ? '' : raw;

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 40;
    input.value = cur;
    input.className = 'thread-rename-input';
    els.threadTitle.dataset.editing = '1';
    els.threadTitle.style.display = 'none';
    els.threadTitle.parentNode.insertBefore(input, els.threadTitle.nextSibling);
    input.focus();
    input.select();

    let settled = false;
    const cleanup = () => {
      input.remove();
      els.threadTitle.style.display = '';
      delete els.threadTitle.dataset.editing;
    };
    const cancel = () => { if (settled) return; settled = true; cleanup(); };
    const commit = async () => {
      if (settled) return; settled = true;
      const name = input.value.trim();
      cleanup();
      if (!name || name === cur) return;
      try {
        await api(`/api/brain/threads/${state.activeId}/name`, {
          method: 'POST', body: JSON.stringify({ name }),
        });
      } catch (e) {
        alert(`Rename failed: ${e.message || e}`);
        return;
      }
      await refreshThreads();
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
    input.addEventListener('blur', commit);
  }

  // ── Send ───────────────────────────────────────────────────────────
  async function sendMessage() {
    const text = els.composerText.value.trim();
    if (!text || state.sending || !state.activeId) return;
    state.sending = true;
    els.sendBtn.disabled = true;
    els.composerText.disabled = true;
    els.typing.classList.add('show');

    // Collect ready attachments (skip any still uploading or in error).
    const sentAttachments = (state.pendingAttachments || [])
      .filter(a => a.status === 'ready' && a.file_id);
    const attachIds = sentAttachments.map(a => a.file_id);

    // Optimistic user message. The send POST is BLOCKING (chat_turn runs the
    // whole OpenClaw turn before returning), so render the attachments INSIDE
    // the bubble — they visibly travel with the message — and clear them from
    // the staging tray immediately. Leaving chips in the tray during the turn
    // made it look like the file hadn't been sent.
    const optimistic = {
      id: 'optimistic-' + Date.now(),
      role: 'user', content: text, surface: 'miniapp',
      created_at: new Date().toISOString(),
      attachments: sentAttachments.map(a => ({ name: a.name, size: a.size })),
    };
    state.messages.push(optimistic);
    renderMessages('force');
    els.composerText.value = '';
    els.composerText.style.height = 'auto';
    // Drop the just-sent attachments from the tray now (keep any still
    // uploading for the next turn).
    state.pendingAttachments = (state.pendingAttachments || [])
      .filter(a => a.status === 'uploading');
    renderAttachments();

    // Wave-2: pass enforce_tool as a separate field so the persisted user
    // content stays clean. brain_wrapper.chat_turn() prepends the directive
    // only when invoking OpenClaw.
    const body = { content: text };
    if (state.pinnedTool) body.enforce_tool = state.pinnedTool;
    if (attachIds.length) body.attachments = attachIds;

    try {
      const resp = await api(`/api/brain/threads/${state.activeId}/messages`, {
        method: 'POST', body: JSON.stringify(body),
      });
      state.messages = state.messages.filter(m => m.id !== optimistic.id);
      if (resp && resp.accepted) {
        // Async turn (HTTP 202): the user row + a reserved assistant row are
        // already persisted. Keep "Dove is thinking" until message.complete
        // arrives over the WS (handleWSEvent clears it). loadMessages shows
        // the real user bubble now; the reply bubble appears on completion.
        state.awaitingReply = {
          thread: state.activeId,
          asstId: resp.assistant_message_id,
        };
        await loadMessages();
        // Safety net: clear the spinner if no completion arrives within the
        // subprocess hard-timeout window (~15 min) so it can't hang forever.
        clearTimeout(state._awaitTimer);
        state._awaitTimer = setTimeout(() => {
          if (state.awaitingReply) {
            state.awaitingReply = null;
            els.typing.classList.remove('show');
            loadMessages().catch(() => {});
          }
        }, 16 * 60 * 1000);
      } else {
        // Legacy synchronous reply (full result in the body).
        await loadMessages();
        const t = state.threads.find(x => x.id === state.activeId);
        if (t) t.message_count = (t.message_count || 0) + 2;
        renderThreads();
      }
    } catch (e) {
      state.messages = state.messages.filter(m => m.id !== optimistic.id);
      if (e.gateway) {
        // The turn outran the edge timeout (CF 524 / 504). The message WAS
        // accepted and OpenClaw may still be finishing — pull the persisted
        // truth rather than calling it a hard failure, and don't re-stage the
        // file (it was sent). The WS will also push the reply when it lands.
        try { await loadMessages(); } catch {}
        renderThreads();
        alert("The reply is taking longer than the gateway allows (524). Your message was sent — Dove may still be working, and the thread will update when the reply lands.");
      } else {
        // Genuine failure (bad request, auth, etc.) — restore the attachments
        // to the tray so the user can retry.
        if (sentAttachments.length) {
          state.pendingAttachments = sentAttachments.concat(state.pendingAttachments || []);
          renderAttachments();
        }
        renderMessages();
        alert(`send failed: ${e.message}`);
      }
    } finally {
      state.sending = false;
      els.sendBtn.disabled = false;
      els.composerText.disabled = false;
      // Keep the spinner while an async reply is still in flight; the WS
      // message.complete handler clears it. Otherwise hide it now.
      if (!state.awaitingReply) els.typing.classList.remove('show');
      els.composerText.focus();
    }
  }

  // ── Context reset — eager summarisation (Wave 2) ──────────────────
  async function resetContext() {
    if (!state.activeId) return;
    if (!confirm("Reset this thread's context?\n\nAll prior messages get compressed into one summary row. The next reply starts fresh from that summary.\n\nThe thread keeps its name and the UI still shows full history; only what the model sees on the next turn changes.")) return;
    try {
      els.resetBtn.disabled = true;
      els.resetBtn.textContent = '↺ Compressing…';
      const r = await api(`/api/brain/threads/${state.activeId}/summarise_all`, { method: 'POST' });
      if (!r.ok) {
        alert(`Couldn't summarise: ${r.reason || 'unknown'}.\n\nMost common cause: LM Studio unreachable or fewer than 2 messages in the thread.`);
      }
      await loadMessages();   // pick up the new summary row
      updateContextCounter(); // counter may drop dramatically
    } catch (e) { alert(`reset failed: ${e.message}`); }
    finally {
      els.resetBtn.disabled = false;
      els.resetBtn.textContent = '↺ Reset context';
    }
  }

  // ── Tiny markdown → safe HTML renderer ────────────────────────────
  // Covers: **bold**, *italic*, `inline code`, ```fenced code```,
  // > blockquote, - / * lists, [text](url), and bare https:// URLs.
  // NOT a full markdown parser — just enough for chat replies.
  function renderMarkdown(src) {
    if (!src) return '';
    // 1) Extract fenced code first so we don't touch its contents.
    const codeBlocks = [];
    let s = src.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_m, lang, body) => {
      const idx = codeBlocks.length;
      codeBlocks.push({ lang, body });
      return `\x00CODEBLOCK${idx}\x00`;
    });
    // 2) Escape HTML on the rest.
    s = esc(s);
    // 3) Inline code  `x`
    s = s.replace(/`([^`\n]+)`/g, (_m, c) => `<code>${c}</code>`);
    // 4) Bold **x** and italic *x* / _x_
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(?<!\w)\*([^*\n]+)\*(?!\w)/g, '<em>$1</em>');
    s = s.replace(/(?<!\w)_([^_\n]+)_(?!\w)/g, '<em>$1</em>');
    // 5) Markdown links [text](url)
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // 6) Bare URLs
    s = s.replace(/(?<!["'>=])(https?:\/\/[^\s<>"]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    // 7) Lists — collapse consecutive "- foo" / "* foo" into <ul>
    s = s.replace(/(?:^|\n)((?:[-*]\s+.+(?:\n|$))+)/g, (_m, block) => {
      const items = block.trim().split(/\n/).map(l => l.replace(/^[-*]\s+/, '').trim());
      return '\n<ul>' + items.map(i => `<li>${i}</li>`).join('') + '</ul>';
    });
    // 8) Blockquotes — "> foo"
    s = s.replace(/(?:^|\n)((?:>\s+.+(?:\n|$))+)/g, (_m, block) => {
      const lines = block.trim().split(/\n/).map(l => l.replace(/^>\s+/, '').trim());
      return '\n<blockquote>' + lines.join('<br>') + '</blockquote>';
    });
    // 9) Restore fenced code blocks (re-escape — they were untouched).
    //    Wrap each in .code-wrap so it can host a language label + copy
    //    button; the `language-*` class lets Prism highlight it (no-op if
    //    Prism's CDN is unavailable — the code just renders plain).
    s = s.replace(/\x00CODEBLOCK(\d+)\x00/g, (_m, i) => {
      const { lang, body } = codeBlocks[+i];
      const langClass = lang ? ` class="language-${esc(lang)}"` : '';
      const langLabel = lang ? `<span class="code-lang">${esc(lang)}</span>` : '';
      return `<div class="code-wrap">${langLabel}<button type="button" class="code-copy" title="Copy code">Copy</button><pre${lang ? ` data-lang="${esc(lang)}"` : ''}><code${langClass}>${esc(body)}</code></pre></div>`;
    });
    return s;
  }

  // ── WS ─────────────────────────────────────────────────────────────
  function _wsUrl() {
    const session = _getSession();
    if (!session) return null;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws/brain?session=${encodeURIComponent(session.token)}`;
  }
  function _sendWS(obj) {
    try { state.ws?.readyState === 1 && state.ws.send(JSON.stringify(obj)); } catch {}
  }
  function _subscribeActive() {
    if (!state.activeId) return;
    const key = `thread:${state.activeId}`;
    const stale = [...state.wsSubs].filter(k => k.startsWith('thread:') && k !== key);
    if (stale.length) {
      _sendWS({ unsubscribe: stale });
      stale.forEach(k => state.wsSubs.delete(k));
    }
    if (!state.wsSubs.has(key)) {
      _sendWS({ subscribe: [key] });
      state.wsSubs.add(key);
    }
  }
  function openWS() {
    const url = _wsUrl();
    if (!url) return;
    if (state.ws && state.ws.readyState <= 1) return;
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.addEventListener('open', () => {
      state.wsBackoff = 1000;
      state.wsSubs.clear();
      _subscribeActive();
    });
    ws.addEventListener('message', (ev) => {
      let msg = null;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleWSEvent(msg);
    });
    ws.addEventListener('close', () => {
      state.ws = null;
      state.wsSubs.clear();
      const wait = Math.min(state.wsBackoff, 30000);
      setTimeout(openWS, wait);
      state.wsBackoff = Math.min(state.wsBackoff * 2, 30000);
    });
    ws.addEventListener('error', () => { /* close fires next */ });
  }
  function handleWSEvent(ev) {
    if (!ev || !ev.kind) return;
    if (ev.kind === 'error' && ev.error === 'auth_required') return showAuthRequired();
    if (ev.kind === 'message.new' || ev.kind === 'message.complete') {
      // An async turn finished — drop the "thinking" spinner. finalize fires
      // message.complete even on bridge_error, so this can't hang.
      if (ev.kind === 'message.complete' && state.awaitingReply &&
          ev.thread_id === state.awaitingReply.thread) {
        state.awaitingReply = null;
        clearTimeout(state._awaitTimer);
        els.typing.classList.remove('show');
      }
      if (ev.thread_id === state.activeId) pullTail();
      const t = state.threads.find(x => x.id === ev.thread_id);
      if (t && ev.kind === 'message.new') {
        t.message_count = (t.message_count || 0) + 1;
        renderThreads();
      }
    } else if (ev.kind === 'thread.updated') {
      refreshThreads();
    }
  }

  // ── Boot ───────────────────────────────────────────────────────────
  async function boot() {
    _adoptApkSession();
    if (!_getSession()) { showAuthRequired(); return; }
    try {
      await refreshThreads();
      if (!state.threads.length) {
        await api('/api/brain/threads', {
          method: 'POST',
          body: JSON.stringify({ name: 'default', kind: 'general', switch: true }),
        });
        await refreshThreads();
      }
      await loadMessages();
      openWS();
      loadInventory(); // fire-and-forget
      // Prism loads via a deferred CDN script, so it may not be ready at
      // first paint. Re-highlight once it lands (and once more as a backstop).
      setTimeout(_highlightAll, 600);
      setTimeout(_highlightAll, 2000);
    } catch (e) {
      if (e.message === 'auth_required') return showAuthRequired();
      els.msgList.innerHTML = `<div class="empty">Failed to load: ${esc(e.message)}</div>`;
    }
  }

  // ── Event wiring ───────────────────────────────────────────────────
  els.composer.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
  els.composerText.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  els.composerText.addEventListener('input', () => {
    els.composerText.style.height = 'auto';
    els.composerText.style.height = Math.min(200, els.composerText.scrollHeight) + 'px';
  });
  if (els.newThreadSide) {
    els.newThreadSide.addEventListener('click', async () => {
      const name = prompt('New thread name?');
      if (name && name.trim()) await createThread(name.trim());
    });
  }

  // ── Attachments + drag/drop ─────────────────────────────────────────
  // Whole block guarded: if the chat.html hasn't been updated to include
  // the attach button + drop overlay, skip the wiring entirely so boot()
  // still runs (and the user sees threads + messages instead of a blank
  // "Loading..." page).
  state.pendingAttachments = []; // [{name, size, file_id, status}]

  function fmtBytes(n) {
    if (!n) return '0 B';
    const u = ['B','KB','MB','GB']; let i = 0;
    while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
    return n.toFixed(n < 10 && i ? 1 : 0) + ' ' + u[i];
  }

  function renderAttachments() {
    const root = els.attachments;
    if (!state.pendingAttachments.length) {
      root.hidden = true; root.innerHTML = '';
      return;
    }
    root.hidden = false;
    root.innerHTML = state.pendingAttachments.map((a, i) => {
      const cls = a.status === 'uploading' ? 'uploading'
                : a.status === 'error'     ? 'error' : '';
      const icon = a.status === 'uploading' ? '⏳'
                 : a.status === 'error'     ? '⚠'
                 : '📄';
      return `<span class="attachment-chip ${cls}" title="${esc(a.name)}${a.error ? ' — ' + esc(a.error) : ''}">
        <span>${icon}</span>
        <span class=name>${esc(a.name)}</span>
        <span class=size>${esc(fmtBytes(a.size))}</span>
        <button type=button class=remove data-i="${i}" title="Remove">✕</button>
      </span>`;
    }).join('');
    root.querySelectorAll('.remove').forEach(btn => {
      btn.addEventListener('click', () => {
        const i = parseInt(btn.dataset.i, 10);
        state.pendingAttachments.splice(i, 1);
        renderAttachments();
      });
    });
  }

  async function uploadFile(file) {
    const entry = {
      name: file.name, size: file.size,
      file_id: null, status: 'uploading',
    };
    state.pendingAttachments.push(entry);
    renderAttachments();
    try {
      const fd = new FormData();
      fd.append('file', file, file.name);
      // Don't use api() here — it adds Content-Type: application/json
      // which clobbers the multipart boundary. Use fetch directly.
      const r = await fetch('/api/brain/upload', {
        method: 'POST', body: fd,
        headers: getAuthHeaders(),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      entry.file_id = j.file_id;
      entry.status  = 'ready';
    } catch (e) {
      entry.status = 'error';
      entry.error  = String(e).slice(0, 80);
    }
    renderAttachments();
  }

  // Auth-header helper that mirrors api()'s injection without forcing JSON
  // content-type (multipart uploads need their own boundary header). The
  // bridge's before_request REQUIRES X-Sentinel-Token in addition to the
  // session token — leaving it off → 401 (the file-upload bug).
  function getAuthHeaders() {
    const h = {};
    if (_sentinelToken) h['X-Sentinel-Token'] = _sentinelToken;
    const sess = _getSession();
    if (sess && sess.token) h['X-Session-Token'] = sess.token;
    else if (window.APK_SESSION_TOKEN) h['X-Session-Token'] = window.APK_SESSION_TOKEN;
    return h;
  }

  if (els.attachBtn && els.fileInput && els.chatMain && els.dropOverlay) {
    // Click the paperclip → open the hidden file picker
    els.attachBtn.addEventListener('click', () => els.fileInput.click());
    els.fileInput.addEventListener('change', (e) => {
      for (const f of e.target.files) uploadFile(f);
      e.target.value = ''; // allow re-picking the same file later
    });

    // Drag-and-drop on the whole chat-main area
    let dragDepth = 0;
    function isFileDrag(e) {
      return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
    }
    els.chatMain.addEventListener('dragenter', (e) => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      dragDepth++;
      els.dropOverlay.hidden = false;
    });
    els.chatMain.addEventListener('dragover', (e) => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    els.chatMain.addEventListener('dragleave', (e) => {
      if (!isFileDrag(e)) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) els.dropOverlay.hidden = true;
    });
    els.chatMain.addEventListener('drop', (e) => {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      dragDepth = 0;
      els.dropOverlay.hidden = true;
      for (const f of (e.dataTransfer.files || [])) uploadFile(f);
    });
  } else {
    console.warn('[chat] attach/drop wiring skipped — HTML missing one of: attachBtn, fileInput, chatMain, dropOverlay');
  }
  els.toolPin.addEventListener('change', (e) => { state.pinnedTool = e.target.value; });
  els.refreshBtn.addEventListener('click', () => loadMessages());

  // Jump-to-latest pill + auto-hide once the user scrolls back to the bottom.
  els.jumpLatest?.addEventListener('click', _scrollToBottom);
  els.msgList.addEventListener('scroll', () => { if (_isNearBottom()) _hideJumpPill(); });

  // Copy buttons (delegated): code-block "Copy" and per-message ⧉.
  els.msgList.addEventListener('click', (e) => {
    const codeBtn = e.target.closest('.code-copy');
    if (codeBtn) {
      const code = codeBtn.parentElement.querySelector('pre code');
      if (code) _copyText(code.textContent, codeBtn);
      return;
    }
    const msgBtn = e.target.closest('.msg-copy');
    if (msgBtn) {
      const row = msgBtn.closest('.msg');
      const m = state.messages.find(x => String(x.id) === (row && row.dataset.msgId));
      if (m) _copyText(m.content || '', msgBtn);
    }
  });
  els.renameBtn?.addEventListener('click', renameActive);
  els.archiveBtn.addEventListener('click', archiveActive);
  els.resetBtn.addEventListener('click', resetContext);
  const openSettings = () => {
    els.settingsModal.hidden = false;
    renderSettingsModal();
  };
  els.settingsLink.addEventListener('click', openSettings);
  els.settingsShortcut?.addEventListener('click', openSettings);

  // Mobile drawer toggles — fired by the hamburger buttons in the header.
  // Backdrop (created by CSS pseudo-element) tap closes whichever is open.
  function _closeDrawers() {
    els.threadsAside.classList.remove('open');
    els.toolsAside.classList.remove('open');
    els.chatApp.classList.remove('drawer-open');
  }
  function _openDrawer(which) {
    _closeDrawers();
    if (which === 'threads') els.threadsAside.classList.add('open');
    else if (which === 'tools') els.toolsAside.classList.add('open');
    els.chatApp.classList.add('drawer-open');
  }
  els.toggleThreads?.addEventListener('click', () => {
    if (els.threadsAside.classList.contains('open')) _closeDrawers();
    else _openDrawer('threads');
  });
  els.toggleTools?.addEventListener('click', () => {
    if (els.toolsAside.classList.contains('open')) _closeDrawers();
    else _openDrawer('tools');
  });
  // Tap backdrop to close. The pseudo-element ::after captures the click,
  // but we listen on chat-app and check if the click was OUTSIDE both drawers.
  els.chatApp.addEventListener('click', (e) => {
    if (!els.chatApp.classList.contains('drawer-open')) return;
    if (els.threadsAside.contains(e.target) || els.toolsAside.contains(e.target)) return;
    if (e.target === els.toggleThreads || e.target === els.toggleTools) return;
    _closeDrawers();
  });
  // Switching threads on mobile should close the drawer for one-hand use.
  // Hook into the existing thread-row click via event delegation:
  els.threadList.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 980px)').matches) _closeDrawers();
  });
  els.settingsClose.addEventListener('click', () => { els.settingsModal.hidden = true; });
  els.settingsModal.addEventListener('click', (e) => {
    if (e.target === els.settingsModal) els.settingsModal.hidden = true;
  });

  boot();
})();
