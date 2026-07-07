/* Sentinel Mini App v2 */
(() => {
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const app            = document.getElementById('app');
  const _sentinelToken = window.SENTINEL_TOKEN || '';
  const _botUsername   = window.BOT_USERNAME   || 'YourSentinelBot';
  const _SESSION_KEY   = 'sentinel_v2_session';
  const _TG_TOKEN_KEY  = 'sentinel_v2_tg_token';

  // ── Toast ────────────────────────────────────────────────────────────────
  function toast(msg) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  function haptic(type = 'light') {
    tg?.HapticFeedback?.impactOccurred(type);
  }

  // ── Session store ────────────────────────────────────────────────────────
  // Phase 1.4 — handle deep-link `?panel=browser` to open straight to browser
  function _maybeDeepLinkPanel() {
    try {
      const params = new URLSearchParams(window.location.search);
      const panel = params.get('panel');
      if (panel && ['browser','memories','reminders','settings','shortcuts'].includes(panel)) {
        return panel;
      }
    } catch {}
    return null;
  }

  function _getSession() {
    try {
      const s = JSON.parse(localStorage.getItem(_SESSION_KEY) || 'null');
      if (s && s.token && s.expires_at * 1000 > Date.now()) return s;
    } catch {}
    return null;
  }

  function _saveSession(token, expires_at) {
    localStorage.setItem(_SESSION_KEY, JSON.stringify({ token, expires_at }));
  }

  // APK suite bootstrap — when the page is served with a valid domain-wide
  // owner cookie (set by Suite launcher /auth/setup), bridge.py mints a fresh
  // session_token and injects it into the page. Adopt it so the existing
  // Telegram-login + TOTP flow is skipped on this device.
  function _adoptApkSession() {
    try {
      const t = window.APK_SESSION_TOKEN;
      if (!t) return false;
      const existing = _getSession();
      if (existing && existing.token === t) return true;
      // bridge.py sets SESSION_TTL to ~8 hours by default. We don't know the
      // exact expiry — match the server's TTL (8h) conservatively. The next
      // call to /api/auth/status will reconcile.
      _saveSession(t, Math.floor(Date.now() / 1000) + 8 * 3600);
      return true;
    } catch { return false; }
  }

  function _clearSession() {
    localStorage.removeItem(_SESSION_KEY);
    localStorage.removeItem(_TG_TOKEN_KEY);
  }

  function _getTgToken() {
    try {
      const s = JSON.parse(localStorage.getItem(_TG_TOKEN_KEY) || 'null');
      if (s && s.token && s.expires_at > Date.now()) return s.token;
    } catch {}
    return null;
  }

  function _saveTgToken(token, expires_in) {
    localStorage.setItem(_TG_TOKEN_KEY, JSON.stringify({
      token, expires_at: Date.now() + expires_in * 1000
    }));
  }

  // ── API ──────────────────────────────────────────────────────────────────
  async function api(path, opts = {}) {
    const session = _getSession();
    const res = await fetch(path, {
      ...opts,
      headers: {
        'Content-Type': 'application/json',
        'X-Sentinel-Token': _sentinelToken,
        ...(session ? { 'X-Session-Token': session.token } : {}),
        ...(opts.headers || {}),
      },
    });
    if (res.status === 401 || res.status === 403) {
      _clearSession();
      renderLogin();
      throw new Error('auth_required');
    }
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  }

  // ── Router ───────────────────────────────────────────────────────────────
  const navStack = [];

  function push(screen, data = {}) {
    navStack.push({ screen, data });
    render(screen, data);
    tg?.BackButton?.show();
  }

  function pop() {
    navStack.pop();
    const prev = navStack[navStack.length - 1] || { screen: 'home', data: {} };
    render(prev.screen, prev.data);
    if (navStack.length <= 1) tg?.BackButton?.hide();
  }

  tg?.BackButton?.onClick(pop);

  function render(screen, data = {}) {
    ({
      home: renderHome, memories: renderMemories, reminders: renderReminders,
      notifications: renderNotifications,
      shortcuts: renderShortcuts, settings: renderSettings,
      'model-select': renderModelSelect, sessions: renderSessions,
      // wd-* screens moved to the new Watchdog Mini App on @YourWatchdogBot.
      // See sentinel-watchdog/BOTS.md for the two-Mini-App split rationale.
      'openclaw-config': renderOpenClawConfig,
      'openclaw-doctor': renderOpenClawDoctor,
      'openclaw-skills': renderOpenClawSkills,
      browser: renderBrowser,
      'tool-drawer': renderToolDrawer,
      'tool-drawer-server': renderToolDrawerServer,
      'watchdog-control': renderWatchdogControl,
      'watchdog-logs': renderWatchdogLogs,
      passkeys: renderPasskeys,
    }[screen] || renderHome)(data);
  }

  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function debounce(fn, ms) {
    let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  // ── Auth: Login screen ───────────────────────────────────────────────────
  function renderLogin() {
    tg?.BackButton?.hide();
    navStack.length = 0;
    app.innerHTML = `
      <div class="login-page">
        <div class="login-logo">⚡</div>
        <div class="login-title">Sentinel</div>
        <div class="login-subtitle">Your personal AI assistant dashboard</div>
        <div id="login-action">
          <div class="spinner" style="margin:32px auto"></div>
        </div>
        <div id="login-error" class="login-error" style="display:none"></div>
        <div id="login-passkey" style="display:none;margin-top:18px">
          <button class="btn btn-secondary" id="passkey-login-btn" style="max-width:240px;margin:0 auto">🔑 Sign in with passkey</button>
        </div>
      </div>`;
    _startTelegramAuth();
    _maybeShowPasskeyLogin();
  }

  async function _maybeShowPasskeyLogin() {
    if (!_passkeySupported()) return;
    try {
      const r = await fetch('/api/auth/webauthn/available', {
        headers: { 'X-Sentinel-Token': _sentinelToken },
      });
      const d = await r.json();
      if (!d.available) return;
      const wrap = document.getElementById('login-passkey');
      const btn  = document.getElementById('passkey-login-btn');
      if (!wrap || !btn) return;
      wrap.style.display = 'block';
      btn.onclick = () => { haptic(); _passkeyLogin(); };
    } catch {}
  }

  async function _startTelegramAuth() {
    const actionEl = document.getElementById('login-action');
    const errEl    = document.getElementById('login-error');

    // Path 1: inside Telegram app — initData is populated and signed
    const initData = tg?.initData;
    if (initData) {
      try {
        const r = await fetch('/api/auth/telegram', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Sentinel-Token': _sentinelToken },
          body: JSON.stringify({ method: 'initdata', init_data: initData }),
        });
        const d = await r.json();
        if (r.status === 429) { _showLoginError(actionEl, errEl, 'Too many attempts — try again in 15 min'); return; }
        if (r.status === 403) { _showLoginError(actionEl, errEl, 'Access denied'); return; }
        if (r.ok && d.tg_token) {
          _saveTgToken(d.tg_token, d.expires_in);
          renderTotp();
          return;
        }
      } catch {}
    }

    // Path 2: browser — show Telegram Login Widget
    actionEl.innerHTML = `
      <div id="tg-widget-wrap" style="margin:24px 0"></div>
      <div class="login-hint">Only the registered owner account can access this dashboard.</div>`;

    const script    = document.createElement('script');
    script.async    = true;
    script.src      = 'https://telegram.org/js/telegram-widget.js?22';
    script.setAttribute('data-telegram-login', _botUsername);
    script.setAttribute('data-size', 'large');
    script.setAttribute('data-onauth', '_onTelegramWidgetAuth(user)');
    script.setAttribute('data-request-access', 'write');
    document.getElementById('tg-widget-wrap').appendChild(script);

    window._onTelegramWidgetAuth = async (user) => {
      actionEl.innerHTML = `<div class="spinner" style="margin:32px auto"></div>`;
      try {
        const r = await fetch('/api/auth/telegram', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Sentinel-Token': _sentinelToken },
          body: JSON.stringify({ method: 'widget', auth_data: user }),
        });
        const d = await r.json();
        if (r.status === 429) { _showLoginError(actionEl, errEl, 'Too many attempts — try again in 15 min'); return; }
        if (r.status === 403) { _showLoginError(actionEl, errEl, 'Access denied — this dashboard is private'); return; }
        if (r.ok && d.tg_token) {
          _saveTgToken(d.tg_token, d.expires_in);
          renderTotp();
          return;
        }
        _showLoginError(actionEl, errEl, 'Authentication failed — try again');
      } catch {
        _showLoginError(actionEl, errEl, 'Network error');
      }
    };
  }

  function _showLoginError(actionEl, errEl, msg) {
    actionEl.innerHTML = `<button class="btn btn-primary" style="max-width:220px;margin:24px auto" id="retry-btn">Try Again</button>`;
    document.getElementById('retry-btn').onclick = () => renderLogin();
    errEl.textContent = msg;
    errEl.style.display = 'block';
  }

  // ── Auth: TOTP screen ────────────────────────────────────────────────────
  function renderTotp(errorMsg = '') {
    tg?.BackButton?.hide();
    app.innerHTML = `
      <div class="totp-page">
        <div class="totp-logo">⚡</div>
        <div class="totp-title">Two-Factor Auth</div>
        <div class="totp-subtitle">Enter your 6-digit code<br>from Google Authenticator</div>
        <input class="totp-input" id="totp-code" type="tel" inputmode="numeric"
               pattern="[0-9]*" maxlength="6" placeholder="000000" autocomplete="one-time-code" />
        <div class="totp-error" id="totp-err" style="visibility:${errorMsg ? 'visible' : 'hidden'}">${errorMsg || '·'}</div>
        <button class="btn btn-primary totp-btn" id="totp-verify">Verify</button>
        <button class="btn btn-ghost totp-back" id="totp-back">← Back</button>
      </div>`;

    const input = document.getElementById('totp-code');
    const errEl = document.getElementById('totp-err');
    const btn   = document.getElementById('totp-verify');

    input.focus();
    input.oninput = () => { if (input.value.length === 6) btn.click(); };

    document.getElementById('totp-back').onclick = () => { _clearSession(); renderLogin(); };

    btn.onclick = async () => {
      const code     = input.value.trim();
      const tg_token = _getTgToken();
      if (code.length !== 6) { errEl.textContent = 'Enter all 6 digits'; errEl.style.visibility = 'visible'; return; }
      if (!tg_token)         { toast('Session expired — please log in again'); renderLogin(); return; }

      btn.textContent = 'Verifying…'; btn.disabled = true;
      try {
        const r = await fetch('/api/auth/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Sentinel-Token': _sentinelToken },
          body: JSON.stringify({ code, tg_token }),
        });
        const d = await r.json();
        if (r.status === 429) {
          errEl.textContent = 'Too many attempts — wait 15 min'; errEl.style.visibility = 'visible';
          btn.textContent = 'Verify'; btn.disabled = false;
          return;
        }
        if (!r.ok || !d.ok) {
          errEl.textContent = d.error === 'tg_token_invalid'
            ? 'Session expired — go back and log in again'
            : 'Wrong code — try again';
          errEl.style.visibility = 'visible';
          input.value = ''; input.focus();
          btn.textContent = 'Verify'; btn.disabled = false;
          haptic('heavy');
          return;
        }
        _saveSession(d.session_token, d.expires_at);
        haptic('medium');
        navStack.push({ screen: 'home', data: {} });
        render('home');
      } catch {
        errEl.textContent = 'Network error';
        errEl.style.visibility = 'visible';
        btn.textContent = 'Verify'; btn.disabled = false;
      }
    };
  }

  // ── Passkeys (WebAuthn) ────────────────────────────────────────────────────
  function _b64urlToBuf(s) {
    s = String(s).replace(/-/g, '+').replace(/_/g, '/');
    const pad = s.length % 4; if (pad) s += '='.repeat(4 - pad);
    const bin = atob(s); const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }
  function _bufToB64url(buf) {
    const bytes = new Uint8Array(buf); let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function _attestationToJSON(cred) {
    const r = cred.response;
    return {
      id: cred.id, type: cred.type, rawId: _bufToB64url(cred.rawId),
      response: {
        clientDataJSON: _bufToB64url(r.clientDataJSON),
        attestationObject: _bufToB64url(r.attestationObject),
        transports: r.getTransports ? r.getTransports() : [],
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    };
  }
  function _assertionToJSON(cred) {
    const r = cred.response;
    return {
      id: cred.id, type: cred.type, rawId: _bufToB64url(cred.rawId),
      response: {
        clientDataJSON: _bufToB64url(r.clientDataJSON),
        authenticatorData: _bufToB64url(r.authenticatorData),
        signature: _bufToB64url(r.signature),
        userHandle: r.userHandle ? _bufToB64url(r.userHandle) : null,
      },
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    };
  }
  const _passkeySupported = () =>
    !!(window.PublicKeyCredential && navigator.credentials && navigator.credentials.create);

  // Login ceremony (pre-session). Returns true on success (session minted).
  async function _passkeyLogin() {
    if (!_passkeySupported()) { toast('Passkeys not supported here'); return false; }
    let opts, handle;
    try {
      const r = await fetch('/api/auth/webauthn/login/options', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Sentinel-Token': _sentinelToken },
      });
      if (r.status === 404) { toast('No passkeys registered'); return false; }
      if (r.status === 429) { toast('Too many attempts — wait 15 min'); return false; }
      if (!r.ok) { toast('Passkey unavailable'); return false; }
      ({ options: opts, handle } = await r.json());
    } catch { toast('Network error'); return false; }
    try {
      opts.challenge = _b64urlToBuf(opts.challenge);
      if (opts.allowCredentials) opts.allowCredentials = opts.allowCredentials.map(c => ({ ...c, id: _b64urlToBuf(c.id) }));
      const assertion = await navigator.credentials.get({ publicKey: opts });
      const vr = await fetch('/api/auth/webauthn/login/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Sentinel-Token': _sentinelToken },
        body: JSON.stringify({ handle, credential: _assertionToJSON(assertion) }),
      });
      const d = await vr.json();
      if (vr.ok && d.session_token) {
        _saveSession(d.session_token, d.expires_at);
        haptic('medium');
        navStack.push({ screen: 'home', data: {} });
        render('home');
        return true;
      }
      toast('Passkey rejected'); return false;
    } catch (e) {
      if (e && e.name === 'NotAllowedError') return false; // user cancelled — silent
      toast('Passkey sign-in failed'); return false;
    }
  }

  // Registration ceremony (session-gated). Returns true on success.
  async function _passkeyRegister(label) {
    if (!_passkeySupported()) { toast('Passkeys not supported here'); return false; }
    let opts, handle;
    try {
      ({ options: opts, handle } = await api('/api/auth/webauthn/register/options', { method: 'POST' }));
    } catch { toast('Could not start registration'); return false; }
    try {
      opts.challenge = _b64urlToBuf(opts.challenge);
      opts.user.id = _b64urlToBuf(opts.user.id);
      if (opts.excludeCredentials) opts.excludeCredentials = opts.excludeCredentials.map(c => ({ ...c, id: _b64urlToBuf(c.id) }));
      const cred = await navigator.credentials.create({ publicKey: opts });
      await api('/api/auth/webauthn/register/verify', {
        method: 'POST',
        body: JSON.stringify({ handle, credential: _attestationToJSON(cred), label }),
      });
      toast('Passkey added'); haptic('medium');
      return true;
    } catch (e) {
      if (e && e.name === 'NotAllowedError') return false; // cancelled
      toast('Could not add passkey'); return false;
    }
  }

  async function renderPasskeys() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">🔑 Passkeys</span></div>
        <div class="card">
          <div style="font-size:13px;color:var(--tg-theme-hint-color);line-height:1.5">
            Passkeys let you sign in with your device's biometrics or PIN instead
            of the Telegram + 2FA flow. They never leave your device.
          </div>
        </div>
        <div id="pk-list"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
        <button class="btn btn-primary section-gap" id="pk-add">+ Add a passkey</button>
      </div>`;
    document.getElementById('pk-add').onclick = async () => {
      haptic();
      const label = (prompt('Name this passkey (e.g. "iPhone", "YubiKey"):', 'Passkey') || '').trim();
      if (label === null) return;
      if (await _passkeyRegister(label || 'Passkey')) loadPasskeys();
    };
    if (!_passkeySupported()) {
      document.getElementById('pk-add').style.display = 'none';
    }
    loadPasskeys();
  }

  async function loadPasskeys() {
    const el = document.getElementById('pk-list');
    if (!el) return;
    try {
      const { credentials } = await api('/api/auth/webauthn/credentials');
      if (!credentials || !credentials.length) {
        el.innerHTML = `<div class="card"><div class="empty-state">No passkeys yet</div></div>`;
        return;
      }
      el.innerHTML = `<div class="card">${credentials.map(c => {
        const added = c.created_at ? new Date(c.created_at * 1000).toLocaleDateString('en-SG', { day: 'numeric', month: 'short', year: 'numeric' }) : '';
        const used  = c.last_used_at ? `last used ${new Date(c.last_used_at * 1000).toLocaleDateString('en-SG', { day: 'numeric', month: 'short' })}` : 'never used';
        const synced = c.backed_up ? ' · synced' : '';
        return `<div class="session-item">
          <div class="session-header">
            <span class="session-icon">🔑</span>
            <div style="flex:1;min-width:0">
              <div class="session-device">${esc(c.label || 'Passkey')}</div>
              <div class="session-meta">added ${esc(added)} · ${esc(used)}${synced}</div>
            </div>
            <button class="memory-delete pk-del" data-id="${esc(c.id)}" title="Remove">×</button>
          </div>
        </div>`;
      }).join('')}</div>`;
      el.querySelectorAll('.pk-del').forEach(btn => {
        btn.onclick = () => deletePasskey(btn.dataset.id);
      });
    } catch { if (el) el.innerHTML = `<div class="card"><div class="empty-state">Failed to load passkeys</div></div>`; }
  }

  async function deletePasskey(credId) {
    if (!confirm('Remove this passkey? You can no longer sign in with it.')) return;
    haptic('medium');
    try {
      await api(`/api/auth/webauthn/credentials/${encodeURIComponent(credId)}`, { method: 'DELETE' });
      toast('Passkey removed'); loadPasskeys();
    } catch { toast('Failed to remove'); }
  }

  // ── Watchdog allowlists + cache REMOVED 2026-05-26 ─────────────────────
  // All wd-* tiles + subpages moved to the new Watchdog Mini App attached to
  // @YourWatchdogBot (hosted at watchdog.your-domain.example.com, sources
  // from /api/v2/* directly). This agent Mini App now stays focused on
  // memories / reminders / OpenClaw doctor / settings. See BOTS.md.

  // ── Home ─────────────────────────────────────────────────────────────────
  let _homeSnap = null; // { status, services, endpoints, ts }

  function _fmtAge(ts) {
    const s = Math.floor((Date.now() - ts) / 1000);
    if (s < 60)  return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  }

  function renderHome() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">⚡ Sentinel</span>
          <span style="display:flex;align-items:center;gap:4px">
            <button class="btn btn-ghost" id="home-notif" style="position:relative;width:auto;padding:4px 10px;font-size:18px">🔔<span id="notif-badge" style="display:none;position:absolute;top:0;right:2px;min-width:16px;height:16px;line-height:16px;padding:0 4px;border-radius:8px;background:#e53935;color:#fff;font-size:10px;font-weight:700;text-align:center"></span></button>
            <button class="btn btn-ghost" id="home-refresh" style="width:auto;padding:4px 10px;font-size:18px">↻</button>
          </span>
        </div>
        <div id="home-status">
          ${_homeSnap ? '' : '<div class="card"><div class="spinner" style="margin:24px auto"></div></div>'}
        </div>
        <div class="card section-gap">
          <div class="card-section-label">Quick Access</div>
          <div class="btn-row">
            <button class="btn btn-secondary" id="nav-mem">🗄 Memories</button>
            <button class="btn btn-secondary" id="nav-rem">⏰ Reminders</button>
          </div>
          <div class="btn-row section-gap">
            <button class="btn btn-secondary" id="nav-br">🌐 Browser</button>
            <button class="btn btn-secondary" id="nav-sc">⚡ Shortcuts</button>
          </div>
          <div class="btn-row section-gap">
            <button class="btn btn-secondary" id="nav-td">🧰 Tool Drawer</button>
            <button class="btn btn-secondary" id="nav-wd">🛡 Watchdog</button>
          </div>
          <div class="btn-row section-gap">
            <button class="btn btn-secondary" id="nav-set">⚙️ Settings</button>
          </div>
        </div>
      </div>`;

    document.getElementById('home-refresh').onclick = () => { haptic(); loadHomeData(true); _refreshNotifBadge(); };
    document.getElementById('home-notif').onclick = () => { haptic(); push('notifications'); };
    _refreshNotifBadge();
    document.getElementById('nav-mem').onclick = () => { haptic(); push('memories'); };
    document.getElementById('nav-rem').onclick = () => { haptic(); push('reminders'); };
    document.getElementById('nav-br').onclick  = () => { haptic(); push('browser'); };
    document.getElementById('nav-sc').onclick  = () => { haptic(); push('shortcuts'); };
    document.getElementById('nav-td').onclick  = () => { haptic(); push('tool-drawer'); };
    document.getElementById('nav-wd').onclick  = () => { haptic(); push('watchdog-control'); };
    document.getElementById('nav-set').onclick = () => { haptic(); push('settings'); };
    if (_homeSnap) {
      _applyHomeSnap(_homeSnap); // paint instantly from cache
    } else {
      loadHomeData();             // first load only
    }
  }

  function _applyHomeSnap(snap) {
    const el = document.getElementById('home-status');
    if (!el) return;

    const { status, services, endpoints, oc_dupe_conflict, ts } = snap;
    const modelShort  = (status.model || 'unknown').split('/').pop();
    const isLocal     = (status.model || '').includes('lmstudio');
    const inferActive = status.inference_active;
    const lmUp        = services.find(s => s.name === 'LM Studio')?.healthy ?? true;
    const modelReady  = isLocal ? lmUp : true;
    const ctxPct      = status.context_pct  || 0;
    const ctxTok      = (status.context_tokens || 0).toLocaleString();
    const memCnt      = status.memory_count  || 0;

    const age = ts ? `<span style="font-size:10px;color:var(--tg-theme-hint-color);float:right">${_fmtAge(ts)}</span>` : '';

    el.innerHTML = `
      <div class="card">
        <div class="card-section-label">Status ${age}</div>
        <div class="status-row">
          <div class="status-dot ${modelReady ? 'active' : 'inactive'}"></div>
          <div class="status-value">${esc(modelShort)}</div>
          <div class="status-label" style="margin-left:auto">${inferActive ? '⚡ inferring' : isLocal ? '🖥 local' : '☁️ cloud'}</div>
        </div>
        <div class="info-grid" style="margin-top:10px">
          <div class="info-cell"><div class="info-value">${memCnt}</div><div class="info-label">Memories</div></div>
          <div class="info-cell"><div class="info-value">${ctxPct}%</div><div class="info-label">Context used</div></div>
        </div>
        <div class="progress-wrap">
          <div class="progress-label"><span>Context</span><span>${ctxTok} tokens</span></div>
          <div class="progress-track"><div class="progress-fill" style="width:${ctxPct}%"></div></div>
        </div>
      </div>`;
  }

  async function loadHomeData(force = false) {
    const el = document.getElementById('home-status');
    if (!el) return;
    if (!force && _homeSnap) { _applyHomeSnap(_homeSnap); return; }
    try {
      const [status, svcData] = await Promise.all([
        api('/api/status'),
        api('/api/services').catch(() => ({ services: [], endpoints: [] })),
      ]);
      _homeSnap = { status, services: svcData.services || [], endpoints: svcData.endpoints || [], oc_dupe_conflict: svcData.oc_dupe_conflict || false, ts: Date.now() };
      _applyHomeSnap(_homeSnap);
    } catch {
      if (el && !_homeSnap) el.innerHTML = `<div class="card"><div class="empty-state">Could not load status</div></div>`;
    }
  }

  // ── Notifications ──────────────────────────────────────────────────────────
  const _NOTIF_ICONS = { info: 'ℹ️', success: '✅', warning: '⚠️', error: '🚨' };

  async function _refreshNotifBadge() {
    const badge = document.getElementById('notif-badge');
    if (!badge) return;
    try {
      const d = await api('/api/notifications');
      const n = d.unread || 0;
      if (n > 0) { badge.textContent = n > 99 ? '99+' : String(n); badge.style.display = 'block'; }
      else       { badge.style.display = 'none'; }
    } catch {}
  }

  function _notifRow(n) {
    const icon = _NOTIF_ICONS[n.level] || '🔔';
    const age  = n.created_at ? _fmtAge(n.created_at * 1000) : '';
    const unread = !n.read_at;
    return `<div class="card section-gap notif-row" data-id="${n.id}" style="cursor:pointer;${unread ? 'border-left:3px solid #e53935' : 'opacity:0.7'}">
      <div class="status-row" style="padding:2px 0">
        <span style="font-size:20px;margin-right:12px;flex-shrink:0">${icon}</span>
        <div style="flex:1;min-width:0">
          ${n.title ? `<div class="memory-subject">${esc(n.title)}</div>` : ''}
          ${n.body ? `<div class="memory-details" style="margin-top:2px;white-space:pre-wrap">${esc(n.body)}</div>` : ''}
        </div>
        <span style="font-size:10px;color:var(--tg-theme-hint-color);flex-shrink:0;margin-left:8px">${age}</span>
      </div>
    </div>`;
  }

  function renderNotifications() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">🔔 Notifications</span>
          <button class="btn btn-ghost" id="notif-readall" style="width:auto;padding:4px 10px;font-size:13px">Mark all read</button>
        </div>
        <div id="notif-list"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    document.getElementById('notif-readall').onclick = async () => {
      haptic();
      try { await api('/api/notifications/read-all', { method: 'POST' }); } catch {}
      await loadNotifications();
      _refreshNotifBadge();
    };
    loadNotifications();
  }

  async function loadNotifications() {
    const el = document.getElementById('notif-list');
    if (!el) return;
    try {
      const d = await api('/api/notifications');
      const list = d.notifications || [];
      if (!list.length) { el.innerHTML = `<div class="card"><div class="empty-state">No notifications</div></div>`; return; }
      el.innerHTML = list.map(_notifRow).join('');
      el.querySelectorAll('.notif-row').forEach(row => {
        row.onclick = async () => {
          const id = row.dataset.id;
          haptic();
          try { await api(`/api/notifications/${id}/read`, { method: 'POST' }); } catch {}
          row.style.borderLeft = ''; row.style.opacity = '0.7';
          _refreshNotifBadge();
        };
      });
    } catch {
      el.innerHTML = `<div class="card"><div class="empty-state">Failed to load notifications</div></div>`;
    }
  }

  // ── Watchdog subpages REMOVED 2026-05-26 ──────────────────────────────
  // _RESTARTABLE, _tagRestartable, _wdItemRow, _renderWdList, _handleRestart,
  // _wdPage, renderWdDocker, renderWdProcs, renderWdHttp, renderWdTunnels,
  // renderWdDisk, renderWdLm, renderWdUpdates, _UPD_CATEGORIES, _updItemRow,
  // _loadUpdates — all moved to the new Watchdog Mini App on
  // @YourWatchdogBot. Sources v2 API directly. See BOTS.md.
  const _RESTARTABLE_REMOVED = true;

  // BEGIN-removed-watchdog-subpages (no-op IIFE to preserve original line spans
  // without dead-code execution; safe to delete once you've confirmed nothing
  // else in the codebase references these names)
  void (() => null);
  /*
  function _tagRestartable(items) {
    return items.map(i => ({ ...i, restartable: _RESTARTABLE.has(i.name) }));
  }

  function _wdItemRow(item) {
    const ok       = item.ok !== undefined ? item.ok : item.healthy;
    const critical = !!item.critical;
    const showBtn  = !ok && !critical && !!item.restartable;
    return `<div class="wd-item${critical ? ' wd-item-critical' : ''}" data-name="${esc(item.name)}">
      <div class="status-dot ${ok ? 'active' : 'inactive'}" style="width:7px;height:7px;flex-shrink:0"></div>
      <div class="wd-item-name">${esc(item.name)}</div>
      <div class="wd-item-detail">${critical ? '<span class="critical-badge">Needs attention</span>' : esc(item.detail || '')}</div>
      ${showBtn ? `<button class="wd-restart-btn" data-name="${esc(item.name)}">Restart</button>` : ''}
    </div>`;
  }

  function _renderWdList(containerId, items, summaryId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const upCount  = items.filter(i => (i.ok !== undefined ? i.ok : i.healthy)).length;
    const downCount = items.length - upCount;
    if (summaryId) {
      const s = document.getElementById(summaryId);
      if (s) s.innerHTML = downCount === 0
        ? `<span style="color:#4cd964">All ${items.length} up</span>`
        : `<span style="color:#ff3b30">${downCount} down</span> of ${items.length}`;
    }
    el.innerHTML = items.length ? items.map(_wdItemRow).join('') : '';
    el.querySelectorAll('.wd-restart-btn').forEach(btn => {
      btn.onclick = () => _handleRestart(btn.dataset.name, items, containerId, summaryId);
    });
  }

  async function _handleRestart(name, items, containerId, summaryId) {
    haptic('medium');
    const item = items.find(i => i.name === name);
    if (!item) return;
    const el  = document.getElementById(containerId);
    const btn = el?.querySelector(`.wd-restart-btn[data-name="${name}"]`);
    if (btn) { btn.textContent = '…'; btn.disabled = true; }
    try {
      const res = await api('/api/service/restart', {
        method: 'POST', body: JSON.stringify({ name }),
      });
      if (res.critical) {
        item.critical = true; item.restartable = false;
        for (const grp of [_wdData?.docker, _wdData?.procs]) {
          const w = grp?.find(i => i.name === name);
          if (w) w.critical = true;
        }
        _renderWdList(containerId, items, summaryId);
        toast(`${name}: restart failed — CRITICAL alert sent`);
        haptic('heavy');
      } else if (res.ok) {
        item.ok = true; item.healthy = true; item.detail = 'running';
        for (const grp of [_wdData?.docker, _wdData?.procs]) {
          const w = grp?.find(i => i.name === name);
          if (w) { w.ok = true; w.healthy = true; }
        }
        _renderWdList(containerId, items, summaryId);
        toast(`${name} restarted`);
        haptic('medium');
      } else {
        if (btn) { btn.textContent = 'Restart'; btn.disabled = false; }
        toast(`Failed: ${res.error || '?'}`);
      }
    } catch {
      if (btn) { btn.textContent = 'Restart'; btn.disabled = false; }
      toast('Restart failed');
    }
  }

  function _wdPage(title, items, emptyMsg) {
    const downCount = items.filter(i => !(i.ok !== undefined ? i.ok : i.healthy)).length;
    const summaryHtml = downCount === 0
      ? `<span style="color:#4cd964">All ${items.length} up</span>`
      : `<span style="color:#ff3b30">${downCount} down</span> of ${items.length}`;
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">${title}</span></div>
        <div class="card">
          <div id="wd-summary" style="font-size:13px;color:var(--tg-theme-hint-color);margin-bottom:10px">${summaryHtml}</div>
          <div id="wd-list">${items.length ? '' : `<div class="empty-state">${emptyMsg}</div>`}</div>
        </div>
      </div>`;
    if (items.length) _renderWdList('wd-list', items, 'wd-summary');
  }

  function renderWdDocker() {
    _wdPage('🐳 Docker', _tagRestartable(_wdData?.docker || []), 'No data — reload home first');
  }

  function renderWdProcs() {
    _wdPage('⚙️ Processes', _tagRestartable(_wdData?.procs || []), 'No data — reload home first');
  }

  function renderWdHttp() {
    _wdPage('🌐 HTTP Endpoints', (_wdData?.http || []).map(i => ({ ...i, restartable: false })), 'No data — reload home first');
  }

  function renderWdTunnels() {
    _wdPage('🛰 Public Tunnels',
            (_wdData?.tunnels || []).map(i => ({ ...i, restartable: false })),
            'No data — reload home first');
  }

  function renderWdDisk() {
    const disk = _wdData?.disk || [];
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">💾 Disk</span></div>
        <div class="card">
          ${disk.length === 0
            ? '<div class="empty-state">No disk info — reload home first</div>'
            : disk.map(d => {
                const pct = d.pct || 0;
                const barColor = pct >= 90 ? '#ff3b30' : (pct >= 75 ? '#ff9500' : '#4cd964');
                return `
                  <div class="settings-row" style="display:block;padding:10px 4px">
                    <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
                      <div style="font-weight:600;font-size:14px">${esc(d.label || '?')}</div>
                      <div style="font-size:12px;color:var(--tg-theme-hint-color);margin-left:auto">
                        ${d.used_gb} / ${d.total_gb} GB
                      </div>
                      <div style="font-weight:700;font-size:14px;color:${barColor};min-width:40px;text-align:right">${pct}%</div>
                    </div>
                    <div class="progress-track" style="margin:0">
                      <div class="progress-fill" style="width:${pct}%;background:${barColor}"></div>
                    </div>
                  </div>`;
              }).join('')}
        </div>
      </div>`;
  }

  function renderWdLm() {
    const lm = _wdData?.lm || [];
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">🧠 LM Studio</span></div>
        <div class="card">
          <div style="font-size:13px;color:var(--tg-theme-hint-color);margin-bottom:10px">
            ${lm.length} model${lm.length === 1 ? '' : 's'} currently loaded
          </div>
          ${lm.length === 0
            ? '<div class="empty-state">LM Studio not responding — start it via the watchdog?</div>'
            : lm.map(m => `
                <div class="settings-row" style="padding:8px 4px;font-family:ui-monospace,Menlo,monospace;font-size:13px">
                  <div class="status-dot active" style="margin-right:8px"></div>
                  <div style="word-break:break-all">${esc(m)}</div>
                </div>`).join('')}
        </div>
      </div>`;
  }

  function renderWdUpdates() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">🔄 Updates</span>
          <button class="btn btn-ghost" id="upd-refresh" style="width:auto;padding:4px 10px;font-size:18px">↻</button>
        </div>
        <div id="upd-list">
          <div class="card"><div class="spinner" style="margin:24px auto"></div></div>
        </div>
      </div>`;
    document.getElementById('upd-refresh').onclick = () => { haptic(); _loadUpdates(); };
    _loadUpdates();
  }

  const _UPD_CATEGORIES = [
    { label: 'AI Core',     names: ['OpenClaw', 'LM Studio'] },
    { label: 'MCP Gateway', names: ['MetaMCP', 'GitHub MCP'] },
    { label: 'Media',       names: ['yt-dlp', 'gallery-dl'] },
    { label: 'Language',    names: ['LibreTranslate'] },
    { label: 'Platform',    names: ['Docker Desktop'] },
  ];

  function _updItemRow(item) {
    const dot   = item.outdated ? '#ff9500' : item.current === '—' ? 'var(--tg-theme-hint-color)' : '#4cd964';
    const badge = item.outdated
      ? `<span style="font-size:10px;color:#ff9500;font-weight:600">UPDATE AVAILABLE</span>`
      : item.current === '—'
        ? `<span style="font-size:10px;color:var(--tg-theme-hint-color)">not found</span>`
        : `<span style="font-size:10px;color:#4cd964">up to date</span>`;
    const updateBtn = item.outdated && item.update_id
      ? `<button class="btn btn-secondary" data-uid="${esc(item.update_id)}"
             style="width:auto;padding:4px 12px;font-size:12px;margin-top:6px">Update</button>`
      : '';
    return `<div class="wd-item" style="flex-direction:column;align-items:flex-start;gap:4px;padding:12px 0">
      <div style="display:flex;align-items:center;gap:8px;width:100%">
        <div class="status-dot" style="background:${dot};width:7px;height:7px;flex-shrink:0"></div>
        <span style="font-weight:600;font-size:14px;flex:1">${esc(item.name)}</span>
        ${badge}
      </div>
      <div style="font-size:12px;color:var(--tg-theme-hint-color);padding-left:15px">
        installed <b>${esc(item.current)}</b> · latest <b>${esc(item.latest)}</b>
      </div>
      ${updateBtn}
    </div>`;
  }

  async function _loadUpdates() {
    const el = document.getElementById('upd-list');
    if (!el) return;
    el.innerHTML = `<div class="card"><div class="spinner" style="margin:24px auto"></div></div>`;
    try {
      const items = await api('/api/updates');
      if (!Array.isArray(items)) throw new Error('bad response');

      const byName = Object.fromEntries(items.map(i => [i.name, i]));
      const seen   = new Set();
      let html = '';

      for (const cat of _UPD_CATEGORIES) {
        const catItems = cat.names.map(n => byName[n]).filter(Boolean);
        if (!catItems.length) continue;
        catItems.forEach(i => seen.add(i.name));
        html += `<div class="card-section-label">${esc(cat.label)}</div>
                 <div class="card">${catItems.map(_updItemRow).join('')}</div>`;
      }

      const rest = items.filter(i => !seen.has(i.name));
      if (rest.length) {
        html += `<div class="card-section-label">Other</div>
                 <div class="card">${rest.map(_updItemRow).join('')}</div>`;
      }

      el.innerHTML = html;

      el.querySelectorAll('[data-uid]').forEach(btn => {
        btn.onclick = async () => {
          haptic('medium');
          const uid  = btn.dataset.uid;
          btn.textContent = 'Updating…'; btn.disabled = true;
          try {
            const res = await api('/api/updates/run', {
              method: 'POST', body: JSON.stringify({ update_id: uid }),
            });
            toast(res.ok ? 'Update complete — refresh to confirm' : `Failed: ${res.error || '?'}`);
            if (res.ok) _loadUpdates();
          } catch {
            toast('Update failed'); btn.textContent = 'Update'; btn.disabled = false;
          }
        };
      });
    } catch {
      el.innerHTML = `<div class="card"><div class="empty-state">Could not fetch versions</div></div>`;
    }
  }
  */
  // END-removed-watchdog-subpages

  // ── Memories ──────────────────────────────────────────────────────────────
  let allMems = [], memPage = 0, addFormOpen = false;
  const MEM_PAGE = 15;

  // Priority-ordered categories — first tag match wins
  const MEM_CATS = [
    { label: 'GitHub',        icon: '🐙', tags: new Set(['github','github-mcp','github-sync','commit','issue','pr','pull-request','repo']) },
    { label: 'Calendar',      icon: '📅', tags: new Set(['calendar','google-calendar','event','schedule','recurring']) },
    { label: 'Finances',      icon: '💰', tags: new Set(['finance','firefly','budget','electricity','power-cost','cost-tracking','sgd']) },
    { label: 'Session Logs',  icon: '📝', tags: new Set(['session-log']) },
    { label: 'Watchdog',      icon: '👁️', tags: new Set(['watchdog','crib-watchdog','power-monitor','gaming','inference','spike-classification','infer-bridge','steam','proxy']) },
    { label: 'Stack & Infra', icon: '🏗️', tags: new Set(['sentinel','architecture','stack','metamcp','openclaw','docker','lmstudio','smdl','fastmcp','miniapp','cloudflare','security','mcp-suffix','wsl2','onedrive','translate-mcp','sentinel-bridge','lifespan','url','tool-naming','memory-mcp','apscheduler','bug','fix']) },
    { label: 'Config',        icon: '⚙️', tags: new Set(['config','reference','routing','tools','monitoring','maps-mcp','reminders-mcp']) },
  ];

  function memCatLabel(mem) {
    const t = new Set(mem.tags || []);
    for (const cat of MEM_CATS) {
      for (const tag of t) { if (cat.tags.has(tag)) return cat.label; }
    }
    return 'Other';
  }

  function parseMemory(content) {
    const raw = (content || '').trim();
    const nl  = raw.indexOf('\n\n');
    if (nl !== -1) return { subject: raw.slice(0, nl).trim(), details: raw.slice(nl + 2).trim() };
    if (raw.length > 72) return { subject: raw.slice(0, 72) + '…', details: raw.slice(72).trim() };
    return { subject: raw, details: '' };
  }

  function renderMemories() {
    addFormOpen = false;
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">🗄 Memories</span>
          <button class="btn btn-ghost" id="mem-add-btn" style="width:auto;padding:4px 10px">+ Add</button>
        </div>
        <input class="search-input" id="mem-search" placeholder="Search all memories…" />
        <div id="add-form-wrap"></div>
        <div id="mem-cats"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    document.getElementById('mem-add-btn').onclick = () => { haptic(); toggleAddForm(); };
    document.getElementById('mem-search').oninput  = debounce(e => {
      const q = e.target.value.trim();
      if (q) renderMemSearch(q);
      else   renderCatLanding();
    }, 400);
    loadAllMems();
  }

  async function loadAllMems() {
    try {
      const result = await api('/api/memories?limit=500');
      allMems = Array.isArray(result) ? result : [];
      renderCatLanding();
    } catch {
      const el = document.getElementById('mem-cats');
      if (el) el.innerHTML = `<div class="card"><div class="empty-state">Failed to load</div></div>`;
    }
  }

  function renderCatLanding() {
    const el = document.getElementById('mem-cats');
    if (!el) return;
    if (!allMems.length) { el.innerHTML = `<div class="card"><div class="empty-state">No memories yet</div></div>`; return; }
    const groups = {};
    for (const m of allMems) {
      const cat = memCatLabel(m);
      (groups[cat] = groups[cat] || []).push(m);
    }
    const orderedLabels = [...MEM_CATS.map(c => c.label), 'Other'].filter(l => groups[l]?.length);
    el.innerHTML = orderedLabels.map(label => {
      const cat   = MEM_CATS.find(c => c.label === label) || { icon: '📌' };
      const count = groups[label].length;
      const latest = groups[label].reduce((a, b) => (b.created_at || 0) > (a.created_at || 0) ? b : a);
      const { subject } = parseMemory(latest.content);
      return `<div class="card mem-cat-card section-gap" data-cat="${esc(label)}" style="cursor:pointer;user-select:none">
        <div class="status-row" style="padding:2px 0">
          <span style="font-size:22px;margin-right:12px;flex-shrink:0">${cat.icon}</span>
          <div style="flex:1;min-width:0">
            <div class="memory-subject">${esc(label)}</div>
            <div class="memory-details" style="margin-top:2px">${esc(subject.slice(0, 60))}${subject.length > 60 ? '…' : ''}</div>
          </div>
          <span class="tag" style="margin:0 8px;flex-shrink:0">${count}</span>
          <span style="color:var(--tg-theme-hint-color);font-size:20px;flex-shrink:0">›</span>
        </div>
      </div>`;
    }).join('');
    el.querySelectorAll('.mem-cat-card').forEach(card => {
      card.onclick = () => { haptic(); renderMemCategory(card.dataset.cat, groups[card.dataset.cat]); };
    });
  }

  function renderMemCategory(label, mems) {
    const cat = MEM_CATS.find(c => c.label === label) || { icon: '📌' };
    memPage = 0;
    const sorted = [...mems].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <button class="btn btn-ghost" id="mem-back-btn" style="width:auto;padding:4px 10px">‹ Back</button>
          <span class="nav-title">${cat.icon} ${esc(label)}</span>
        </div>
        <div class="card" id="mem-list"></div>
      </div>`;
    document.getElementById('mem-back-btn').onclick = () => { haptic(); renderMemories(); };
    renderMemSubList(sorted);
  }

  function renderMemSubList(mems) {
    const list = document.getElementById('mem-list');
    if (!list) return;
    if (!mems.length) { list.innerHTML = `<div class="empty-state">No memories</div>`; return; }
    const slice = mems.slice(0, (memPage + 1) * MEM_PAGE);
    list.innerHTML = slice.map(m => {
      const { subject, details } = parseMemory(m.content);
      const tags = (m.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('');
      const dt   = m.created_at
        ? new Date(m.created_at).toLocaleDateString('en-SG', { day: 'numeric', month: 'short' })
        : '';
      return `<div class="memory-item">
        <div class="memory-subject">${esc(subject)}</div>
        ${details ? `<div class="memory-details">${esc(details.slice(0, 120))}${details.length > 120 ? '…' : ''}</div>` : ''}
        <div class="memory-meta">
          ${tags}
          ${dt ? `<span class="memory-time">${dt}</span>` : ''}
          <button class="memory-delete" data-id="${m.id}" title="Delete">×</button>
        </div>
      </div>`;
    }).join('');
    if (mems.length > slice.length) {
      list.insertAdjacentHTML('beforeend',
        `<div class="btn btn-ghost section-gap" id="load-more-btn">Load more (${mems.length - slice.length} left)</div>`);
      document.getElementById('load-more-btn').onclick = () => { memPage++; renderMemSubList(mems); };
    }
    list.querySelectorAll('.memory-delete').forEach(btn => {
      btn.onclick = () => deleteMemSub(parseInt(btn.dataset.id, 10), mems);
    });
  }

  async function deleteMemSub(id, mems) {
    try {
      await api(`/api/memories/${id}`, { method: 'DELETE' });
      haptic('medium'); toast('Deleted');
      allMems = allMems.filter(m => m.id !== id);
      const remaining = mems.filter(m => m.id !== id);
      if (!remaining.length) renderMemories();
      else renderMemSubList(remaining);
    } catch { toast('Failed to delete'); }
  }

  async function renderMemSearch(q) {
    const el = document.getElementById('mem-cats');
    if (!el) return;
    el.innerHTML = `<div class="card"><div class="spinner" style="margin:24px auto"></div></div>`;
    try {
      const result = await api(`/api/memories?q=${encodeURIComponent(q)}&limit=100`);
      const mems = Array.isArray(result) ? result : [];
      memPage = 0;
      if (!mems.length) { el.innerHTML = `<div class="card"><div class="empty-state">No results for "${esc(q)}"</div></div>`; return; }
      const sorted = [...mems].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      el.innerHTML = `<div class="card" id="mem-list"></div>`;
      renderMemSubList(sorted);
    } catch { el.innerHTML = `<div class="card"><div class="empty-state">Search failed</div></div>`; }
  }

  function toggleAddForm() {
    addFormOpen = !addFormOpen;
    const wrap = document.getElementById('add-form-wrap');
    if (addFormOpen) {
      wrap.innerHTML = `
        <div class="card section-gap">
          <div class="add-memory-form">
            <input class="tags-input" id="mem-subject" placeholder="Subject (e.g. Window seat preference)" />
            <textarea id="mem-text" placeholder="Details (optional — extra context, specifics)"></textarea>
            <input class="tags-input" id="mem-tags" placeholder="Tags (comma-separated, e.g. preference, config)" />
            <div class="btn-row">
              <button class="btn btn-secondary" id="mem-form-cancel">Cancel</button>
              <button class="btn btn-primary" id="mem-form-save">Save</button>
            </div>
          </div>
        </div>`;
      document.getElementById('mem-form-cancel').onclick = () => { haptic(); addFormOpen = false; wrap.innerHTML = ''; };
      document.getElementById('mem-form-save').onclick   = saveMemory;
      document.getElementById('mem-subject').focus();
    } else {
      wrap.innerHTML = '';
    }
  }

  async function saveMemory() {
    const subject = document.getElementById('mem-subject')?.value?.trim();
    const details = document.getElementById('mem-text')?.value?.trim();
    if (!subject) { toast('Enter a subject first'); return; }
    const content = details ? `${subject}\n\n${details}` : subject;
    const raw     = document.getElementById('mem-tags')?.value?.trim();
    const tags    = raw ? raw.split(',').map(t => t.trim()).filter(Boolean) : [];
    const btn     = document.getElementById('mem-form-save');
    btn.textContent = 'Saving…'; btn.disabled = true;
    try {
      await api('/api/memories', { method: 'POST', body: JSON.stringify({ content, tags }) });
      haptic('medium'); toast('Memory saved');
      addFormOpen = false;
      document.getElementById('add-form-wrap').innerHTML = '';
      await loadAllMems();
    } catch { toast('Failed to save'); btn.textContent = 'Save'; btn.disabled = false; }
  }

  // ── Reminders ─────────────────────────────────────────────────────────────
  let allRems = [], remAddOpen = false, remTarget = 'dm', remContacts = [], remSelectedContacts = new Set();

  function formatNextRun(iso) {
    if (!iso) return '';
    const d = new Date(iso), now = new Date();
    const tod = new Date(now); tod.setHours(0,0,0,0);
    const tom = new Date(tod); tom.setDate(tom.getDate() + 1);
    const aft = new Date(tod); aft.setDate(aft.getDate() + 2);
    const time = d.toLocaleTimeString('en-SG', { hour: '2-digit', minute: '2-digit' });
    if (d >= tod && d < tom) return `Today at ${time}`;
    if (d >= tom && d < aft) return `Tomorrow at ${time}`;
    const days = Math.round((d - now) / 86400000);
    if (days < 7) return `${d.toLocaleDateString('en-SG', { weekday: 'short' })} at ${time}`;
    return `${d.toLocaleDateString('en-SG', { day: 'numeric', month: 'short' })} at ${time}`;
  }

  function renderReminders() {
    remAddOpen = false; remTarget = 'dm'; remContacts = []; remSelectedContacts = new Set();
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">⏰ Reminders</span>
          <button class="btn btn-ghost" id="rem-add-btn" style="width:auto;padding:4px 10px">+ Add</button>
        </div>
        <div id="rem-add-wrap"></div>
        <div id="rem-cats"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    document.getElementById('rem-add-btn').onclick = () => { haptic(); toggleRemAdd(); };
    loadReminders();
  }

  async function toggleRemAdd() {
    remAddOpen = !remAddOpen;
    const wrap = document.getElementById('rem-add-wrap');
    if (remAddOpen) {
      try { remContacts = await api('/api/contacts'); } catch { remContacts = []; }
      if (!Array.isArray(remContacts)) remContacts = [];
      _renderRemForm(wrap);
    } else { wrap.innerHTML = ''; }
  }

  function _contactsListHtml() {
    if (!remContacts.length) {
      return `<div style="font-size:13px;color:var(--tg-theme-hint-color);padding:10px 4px">
        No contacts yet — have someone send /start to the bot.</div>`;
    }
    return remContacts.map(c => {
      const label   = c.first_name || c.username || c.chat_id;
      const sub     = c.username ? `@${c.username}` : `ID ${c.chat_id}`;
      const checked = remSelectedContacts.has(c.chat_id) ? 'checked' : '';
      return `<label class="rem-contact-row" data-id="${esc(c.chat_id)}">
        <input type="checkbox" class="rem-contact-cb" value="${esc(c.chat_id)}" ${checked} />
        <span class="rem-contact-name">${esc(label)}</span>
        <span class="rem-contact-sub">${esc(sub)}</span>
      </label>`;
    }).join('');
  }

  function _renderRemForm(wrap) {
    wrap.innerHTML = `
      <div class="card section-gap">
        <div class="card-section-label">New Reminder</div>
        <div class="add-memory-form">
          <textarea id="rem-msg" placeholder="Reminder message (e.g. Take medication)"></textarea>
          <input class="tags-input" id="rem-when" placeholder="When (e.g. tomorrow 9am, every Monday at 8am)" />
          <input class="tags-input" id="rem-label" placeholder="Label (optional)" />
          <div class="card-section-label" style="margin-top:8px;margin-bottom:4px">Send to</div>
          <div class="btn-row" style="margin-top:0">
            <button class="btn ${remTarget==='dm'?'btn-primary':'btn-secondary'}" id="rem-target-dm">Me</button>
            <button class="btn ${remTarget==='group'?'btn-primary':'btn-secondary'}" id="rem-target-group">Group</button>
            <button class="btn ${remTarget==='contacts'?'btn-primary':'btn-secondary'}" id="rem-target-contacts">Contacts</button>
          </div>
          <div id="rem-contact-wrap" style="display:${remTarget==='contacts'?'block':'none'}">
            <div id="rem-contact-list" style="margin-top:8px">${_contactsListHtml()}</div>
          </div>
          <div class="btn-row section-gap">
            <button class="btn btn-secondary" id="rem-form-cancel">Cancel</button>
            <button class="btn btn-primary"   id="rem-form-save">Set Reminder</button>
          </div>
        </div>
      </div>`;
    document.getElementById('rem-target-dm').onclick       = () => { remTarget = 'dm';       rerenderTargetBtns(); haptic(); };
    document.getElementById('rem-target-group').onclick    = () => { remTarget = 'group';    rerenderTargetBtns(); haptic(); };
    document.getElementById('rem-target-contacts').onclick = () => { remTarget = 'contacts'; rerenderTargetBtns(); haptic(); };
    document.getElementById('rem-form-cancel').onclick     = () => { haptic(); remAddOpen = false; wrap.innerHTML = ''; };
    document.getElementById('rem-form-save').onclick       = saveReminder;
    wrap.querySelectorAll('.rem-contact-cb').forEach(cb => {
      cb.onchange = () => {
        if (cb.checked) remSelectedContacts.add(cb.value);
        else            remSelectedContacts.delete(cb.value);
      };
    });
    document.getElementById('rem-msg').focus();
  }

  function rerenderTargetBtns() {
    const dm  = document.getElementById('rem-target-dm');
    const grp = document.getElementById('rem-target-group');
    const con = document.getElementById('rem-target-contacts');
    const cw  = document.getElementById('rem-contact-wrap');
    if (dm)  dm.className  = `btn ${remTarget === 'dm'       ? 'btn-primary' : 'btn-secondary'}`;
    if (grp) grp.className = `btn ${remTarget === 'group'    ? 'btn-primary' : 'btn-secondary'}`;
    if (con) con.className = `btn ${remTarget === 'contacts' ? 'btn-primary' : 'btn-secondary'}`;
    if (cw)  cw.style.display = remTarget === 'contacts' ? 'block' : 'none';
  }

  async function saveReminder() {
    const message     = document.getElementById('rem-msg')?.value?.trim();
    const when        = document.getElementById('rem-when')?.value?.trim();
    const label       = document.getElementById('rem-label')?.value?.trim();
    const contact_ids = remTarget === 'contacts' ? [...remSelectedContacts] : [];
    if (!message) { toast('Enter a reminder message'); return; }
    if (!when)    { toast('Enter when to send it'); return; }
    if (remTarget === 'contacts' && !contact_ids.length) { toast('Select at least one contact'); return; }
    const btn = document.getElementById('rem-form-save');
    btn.textContent = 'Setting…'; btn.disabled = true;
    try {
      const body = { message, when, label, target: remTarget };
      if (contact_ids.length) body.contact_ids = contact_ids;
      const result = await api('/api/reminders', { method: 'POST', body: JSON.stringify(body) });
      if (result.error) { toast(`Error: ${result.error}`); btn.textContent = 'Set Reminder'; btn.disabled = false; return; }
      haptic('medium'); toast(`Reminder set — ${result.schedule || 'scheduled'}`);
      remAddOpen = false; remSelectedContacts = new Set();
      document.getElementById('rem-add-wrap').innerHTML = '';
      await loadReminders();
    } catch { toast('Failed to set reminder'); btn.textContent = 'Set Reminder'; btn.disabled = false; }
  }

  async function loadReminders() {
    try {
      allRems = await api('/api/reminders');
      if (!Array.isArray(allRems)) allRems = [];
      renderRemCatLanding();
    } catch {
      const el = document.getElementById('rem-cats');
      if (el) el.innerHTML = `<div class="card"><div class="empty-state">Failed to load reminders</div></div>`;
    }
  }

  function renderRemCatLanding() {
    const el = document.getElementById('rem-cats');
    if (!el) return;
    if (!allRems.length) { el.innerHTML = `<div class="card"><div class="empty-state">No active reminders</div></div>`; return; }
    const recurring = allRems.filter(r => r.trigger_type !== 'date');
    const onetime   = allRems.filter(r => r.trigger_type === 'date');
    const cats = [
      { label: 'Recurring', icon: '🔁', rems: recurring },
      { label: 'One-time',  icon: '⏰', rems: onetime   },
    ].filter(c => c.rems.length);
    el.innerHTML = cats.map(cat => {
      const next = [...cat.rems].sort((a, b) => (!a.next_run ? 1 : !b.next_run ? -1 : new Date(a.next_run) - new Date(b.next_run)))[0];
      const preview = next ? (next.label || (next.message || '').slice(0, 50)) : '';
      const nextStr = next?.next_run ? formatNextRun(next.next_run) : '';
      return `<div class="card mem-cat-card section-gap" data-cat="${esc(cat.label)}" style="cursor:pointer;user-select:none">
        <div class="status-row" style="padding:2px 0">
          <span style="font-size:22px;margin-right:12px;flex-shrink:0">${cat.icon}</span>
          <div style="flex:1;min-width:0">
            <div class="memory-subject">${esc(cat.label)}</div>
            <div class="memory-details" style="margin-top:2px">${esc(preview.slice(0, 55))}${preview.length > 55 ? '…' : ''}${nextStr ? ` · ${nextStr}` : ''}</div>
          </div>
          <span class="tag" style="margin:0 8px;flex-shrink:0">${cat.rems.length}</span>
          <span style="color:var(--tg-theme-hint-color);font-size:20px;flex-shrink:0">›</span>
        </div>
      </div>`;
    }).join('');
    el.querySelectorAll('.mem-cat-card').forEach(card => {
      const cat = cats.find(c => c.label === card.dataset.cat);
      card.onclick = () => { haptic(); renderRemCategory(cat); };
    });
  }

  function renderRemCategory(cat) {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <button class="btn btn-ghost" id="rem-back-btn" style="width:auto;padding:4px 10px">‹ Back</button>
          <span class="nav-title">${cat.icon} ${esc(cat.label)}</span>
        </div>
        <div id="rem-list"></div>
      </div>`;
    document.getElementById('rem-back-btn').onclick = () => { haptic(); renderReminders(); };
    renderRemSubList(cat.rems, cat.label);
  }

  function renderRemSubList(rems, catLabel) {
    const el = document.getElementById('rem-list');
    if (!el) return;
    const sorted = [...rems].sort((a, b) => (!a.next_run ? 1 : !b.next_run ? -1 : new Date(a.next_run) - new Date(b.next_run)));
    el.innerHTML = `<div class="card">${sorted.map(r => {
      const isRecurring = r.trigger_type !== 'date';
      const ico      = isRecurring ? '🔁' : '⏰';
      const title    = esc(r.label || (r.message || '').slice(0, 60));
      const schedule = esc(r.trigger_description || r.when_raw || '');
      const nextRun  = r.next_run ? `Next: ${formatNextRun(r.next_run)}` : '';
      const target   = (r.chat_id && !String(r.chat_id).startsWith('-')) ? 'DM' : 'Group';
      return `<div class="memory-item">
        <div class="memory-content"><span style="margin-right:6px">${ico}</span>${title}</div>
        <div style="font-size:12px;color:var(--tg-theme-hint-color);margin:4px 0 2px">${schedule}</div>
        <div class="memory-meta">
          ${nextRun ? `<span style="font-size:11px;color:var(--tg-theme-hint-color)">${nextRun}</span>` : ''}
          <span class="tag">${target}</span>
          <button class="memory-delete" data-id="${esc(r.id)}" title="Cancel">×</button>
        </div>
      </div>`;
    }).join('')}</div>`;
    el.querySelectorAll('.memory-delete').forEach(btn => {
      btn.onclick = () => cancelReminderSub(btn.dataset.id, rems, catLabel);
    });
  }

  async function cancelReminderSub(id, rems, catLabel) {
    try {
      await api(`/api/reminders/${encodeURIComponent(id)}`, { method: 'DELETE' });
      haptic('medium'); toast('Reminder cancelled');
      allRems = allRems.filter(r => r.id !== id);
      const remaining = rems.filter(r => r.id !== id);
      if (!remaining.length) renderReminders();
      else renderRemSubList(remaining, catLabel);
    } catch { toast('Failed to cancel'); }
  }

  // ── Shortcuts ─────────────────────────────────────────────────────────────
  // Auto-send default: direct-fire on tap via Telethon (one-tap, mini-app stays open).
  // Toggle off → falls back to switchInlineQuery (closes mini-app, pre-fills chat for manual edit).
  function _scAutoSend() {
    return localStorage.getItem('sc-auto-send') !== 'false';
  }
  function _scSetAutoSend(on) {
    localStorage.setItem('sc-auto-send', on ? 'true' : 'false');
  }

  async function _scFire(prompt) {
    haptic();
    if (_scAutoSend()) {
      // Direct send via Telethon — same path as the V3 chat composer
      try {
        await api('/api/agent/message', {
          method: 'POST', body: JSON.stringify({ text: prompt }),
        });
        toast('Sent — reply lands in your Telegram chat');
      } catch (e) {
        // Fallback: classic switchInlineQuery / clipboard if Telethon unavailable
        toast('Auto-send failed — opening chat for manual send');
        if (tg?.switchInlineQuery) {
          tg.switchInlineQuery(prompt);
        } else {
          navigator.clipboard.writeText(prompt)
            .then(() => setTimeout(() => tg?.close(), 500))
            .catch(() => {});
        }
      }
    } else {
      // Manual mode: pre-fill chat input, user reviews/edits + presses send
      if (tg?.switchInlineQuery) {
        tg.switchInlineQuery(prompt);
      } else {
        navigator.clipboard.writeText(prompt)
          .then(() => { toast('Copied — returning to chat…'); setTimeout(() => tg?.close(), 500); })
          .catch(() => toast('Copy failed'));
      }
    }
  }

  // ── Browser panel (V3 Phase 1.0 viewer + 1.1 scrubber + 1.2 hold + 1.3 copilot) ──
  let _browserSSE = null;
  let _browserLastFrameTs = 0;
  let _browserFrames = [];          // [{ts, jpeg, src}]
  const _BROWSER_MAX_FRAMES = 120;
  let _browserScrubMode = false;
  let _browserCopilotMode = false;  // 1.3: tap+type forwarding to Playwright
  // Last frame's natural pixel dimensions — used to scale canvas clicks to page coords
  let _browserFrameW = 0;
  let _browserFrameH = 0;

  function renderBrowser() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">🌐 Browser</span>
          <button class="btn btn-ghost" id="browser-copilot-btn" style="margin-left:auto;width:auto;padding:4px 10px;font-size:12px">🖱 Drive</button>
          <button class="btn btn-ghost" id="browser-pause-btn" style="margin-left:6px;width:auto;padding:4px 10px;font-size:12px">⏸ Hold</button>
          <span id="browser-status" class="status-label" style="margin-left:8px;font-size:11px">Connecting…</span>
        </div>
        <div class="card section-gap" style="padding:0;overflow:hidden">
          <div id="browser-info" style="padding:8px 12px;font-size:11px;color:var(--tg-theme-hint-color);border-bottom:1px solid rgba(255,255,255,0.06);min-height:18px">
            <span id="browser-title">Waiting for agent to use the browser</span>
          </div>
          <canvas id="browser-canvas" style="display:block;width:100%;height:auto;background:#000"></canvas>
          <div id="browser-empty" style="padding:40px 20px;text-align:center;color:var(--tg-theme-hint-color);font-size:13px;display:none">
            Frames appear when the agent uses the browser.<br>
            <span style="font-size:11px;opacity:0.7">Ask the bot to "search the weather in Singapore" or "navigate to example.com"</span>
          </div>
          <div id="browser-scrub-bar" style="display:none;padding:8px 12px;background:rgba(0,0,0,0.2);border-top:1px solid rgba(255,255,255,0.06)">
            <input type="range" id="browser-scrub" min="0" max="0" value="0" step="1"
                   style="width:100%;accent-color:var(--tg-theme-button-color);cursor:pointer" />
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;font-size:11px;color:var(--tg-theme-hint-color)">
              <span id="browser-scrub-pos">Live</span>
              <button class="btn btn-ghost" id="browser-live-btn" style="display:none;width:auto;padding:2px 10px;font-size:11px">⏵ Live</button>
            </div>
          </div>
          <div id="browser-copilot-bar" style="display:none;padding:8px 12px;background:rgba(0,128,255,0.08);border-top:1px solid rgba(0,128,255,0.2)">
            <div style="font-size:11px;color:var(--tg-theme-button-color);margin-bottom:6px;font-weight:600">🖱 Drive Mode — tap canvas to click; type below to input</div>
            <div style="display:flex;gap:6px;align-items:center">
              <input type="text" id="browser-input" class="tags-input" placeholder="Type to focused element + Enter…"
                     style="flex:1;padding:6px 10px;font-size:13px" />
              <button class="btn btn-secondary" id="browser-key-enter" style="width:auto;padding:6px 10px;font-size:11px">↵</button>
              <button class="btn btn-secondary" id="browser-key-tab" style="width:auto;padding:6px 10px;font-size:11px">⇥</button>
              <button class="btn btn-secondary" id="browser-key-esc" style="width:auto;padding:6px 10px;font-size:11px">esc</button>
              <button class="btn btn-secondary" id="browser-cookies-import" title="Import your Chrome cookies for the current site so the agent inherits your login" style="width:auto;padding:6px 10px;font-size:11px">🍪</button>
            </div>
            <div style="font-size:10px;color:var(--tg-theme-hint-color);margin-top:4px">
              ⚠ Anything you type here is visible to the agent (it sees the page).
            </div>
          </div>
          <div id="browser-agent-bar" style="padding:8px 12px;background:rgba(0,200,100,0.06);border-top:1px solid rgba(0,200,100,0.2)">
            <div style="font-size:11px;color:var(--tg-theme-hint-color);margin-bottom:6px">💬 Tell agent what to do (sent via your Telegram, reply lands in chat)</div>
            <div style="display:flex;gap:6px;align-items:flex-end">
              <textarea id="browser-agent-input" class="tags-input" placeholder="e.g. navigate to bbc.com — click the second headline"
                        style="flex:1;padding:8px 10px;font-size:13px;resize:vertical;min-height:36px;max-height:120px"></textarea>
              <button class="btn btn-primary" id="browser-agent-send" style="width:auto;padding:8px 14px;font-size:13px">Send</button>
            </div>
          </div>
        </div>
      </div>`;

    // Reset state for fresh viewing session
    _browserFrames = [];
    _browserScrubMode = false;

    // Wire scrub controls
    const scrub = document.getElementById('browser-scrub');
    const liveBtn = document.getElementById('browser-live-btn');
    scrub.addEventListener('input', () => {
      _browserScrubMode = parseInt(scrub.value, 10) < _browserFrames.length - 1;
      _browserShowFrameAt(parseInt(scrub.value, 10));
      liveBtn.style.display = _browserScrubMode ? 'inline-block' : 'none';
    });
    liveBtn.addEventListener('click', () => {
      haptic();
      _browserScrubMode = false;
      scrub.value = _browserFrames.length - 1;
      liveBtn.style.display = 'none';
      _browserShowFrameAt(_browserFrames.length - 1);
    });

    // Pause/Hold button: freeze the canvas at the current latest frame
    // (does NOT pause the agent — just stops auto-advancing the view here)
    const pauseBtn = document.getElementById('browser-pause-btn');
    pauseBtn.addEventListener('click', () => {
      haptic();
      if (_browserScrubMode) {
        // Already paused/scrubbing — resume live
        _browserScrubMode = false;
        scrub.value = _browserFrames.length - 1;
        liveBtn.style.display = 'none';
        pauseBtn.textContent = '⏸ Hold';
        _browserShowFrameAt(_browserFrames.length - 1);
      } else {
        // Pause at current latest
        _browserScrubMode = true;
        scrub.value = _browserFrames.length - 1;
        liveBtn.style.display = 'inline-block';
        pauseBtn.textContent = '▶ Resume';
        _browserShowFrameAt(_browserFrames.length - 1);
      }
    });

    // Phase 1.3 — co-pilot wiring
    document.getElementById('browser-copilot-btn').addEventListener('click', _browserToggleCopilot);

    const canvas = document.getElementById('browser-canvas');
    canvas.addEventListener('click', (e) => {
      if (!_browserCopilotMode) return;
      const rect = canvas.getBoundingClientRect();
      _browserSendClick(e.clientX - rect.left, e.clientY - rect.top);
    });

    const inputEl = document.getElementById('browser-input');
    inputEl.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter' && inputEl.value.trim()) {
        e.preventDefault();
        const text = inputEl.value;
        inputEl.value = '';
        await _browserSendType(text);
        await _browserSendKey('Enter');
      }
    });

    document.getElementById('browser-key-enter').addEventListener('click', async () => {
      if (inputEl.value.trim()) {
        await _browserSendType(inputEl.value);
        inputEl.value = '';
      }
      await _browserSendKey('Enter');
    });
    document.getElementById('browser-key-tab').addEventListener('click', () => _browserSendKey('Tab'));
    document.getElementById('browser-key-esc').addEventListener('click', () => _browserSendKey('Escape'));

    // Import Chrome cookies for current site — bypasses login walls (not Turnstile)
    document.getElementById('browser-cookies-import').addEventListener('click', async () => {
      haptic('medium');
      const btn = document.getElementById('browser-cookies-import');
      const orig = btn.textContent;
      btn.textContent = '…';
      btn.disabled = true;
      try {
        const r = await api('/api/browser/import-cookies', {
          method: 'POST', body: JSON.stringify({}),  // domain inferred from current URL
        });
        if (r.imported > 0) {
          toast(`Imported ${r.imported} cookie${r.imported>1?'s':''} for ${r.domain}`);
        } else {
          toast(`No cookies found for ${r.domain || 'this site'} in your Chrome profile`);
        }
      } catch (e) {
        toast('Cookie import failed — see console');
        console.error(e);
      } finally {
        btn.textContent = orig;
        btn.disabled = false;
      }
    });

    // Phase 1.5 — agent chat composer (Telethon → AI bot DM as the owner)
    const agentInput = document.getElementById('browser-agent-input');
    const agentSend = document.getElementById('browser-agent-send');
    const sendAgent = async () => {
      const text = agentInput.value.trim();
      if (!text) return;
      haptic('medium');
      agentSend.textContent = '…';
      agentSend.disabled = true;
      try {
        const r = await api('/api/agent/message', {
          method: 'POST', body: JSON.stringify({ text }),
        });
        if (r.ok) {
          toast('Sent — reply lands in your Telegram chat');
          agentInput.value = '';
        } else {
          toast(`Error: ${r.error || 'send failed'}`);
        }
      } catch { toast('Send failed'); }
      finally {
        agentSend.textContent = 'Send';
        agentSend.disabled = false;
      }
    };
    agentSend.addEventListener('click', sendAgent);
    agentInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendAgent(); }
    });

    _browserStartStream();
    window.addEventListener('popstate', _browserStopStream, { once: true });
  }

  function _browserShowFrameAt(idx) {
    const frame = _browserFrames[idx];
    if (!frame) return;
    _browserDrawFrame(frame.jpeg);
    const tEl = document.getElementById('browser-title');
    const posEl = document.getElementById('browser-scrub-pos');
    if (tEl) {
      const m = (frame.src || '').match(/T(\d{2})-(\d{2})-(\d{2})/);
      tEl.textContent = m ? `Capture at ${m[1]}:${m[2]}:${m[3]} UTC` : 'Live capture';
    }
    if (posEl) {
      const isLive = idx === _browserFrames.length - 1;
      const secsAgo = Math.round((Date.now() / 1000 - frame.ts));
      posEl.textContent = isLive ? 'Live' : `${secsAgo}s ago (frame ${idx + 1}/${_browserFrames.length})`;
    }
  }

  function _browserStartStream() {
    _browserStopStream();  // safety: kill any existing
    const session = _getSession();
    if (!session) { _browserSetStatus('Auth lost'); return; }

    const url = `/api/browser/stream?session=${encodeURIComponent(session.token)}`;
    const es = new EventSource(url);
    _browserSSE = es;

    let frameCount = 0;
    const startTs = Date.now();

    es.onopen = () => _browserSetStatus('Connected');
    es.onerror = (e) => {
      _browserSetStatus('Disconnected — retrying');
      // EventSource auto-reconnects; nothing to do
    };
    es.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data);
        if (!frame.jpeg) return;

        // Push into ring buffer (drop oldest if over cap)
        _browserFrames.push(frame);
        if (_browserFrames.length > _BROWSER_MAX_FRAMES) _browserFrames.shift();

        // Update scrubber max range
        const scrub = document.getElementById('browser-scrub');
        const bar = document.getElementById('browser-scrub-bar');
        if (scrub) {
          scrub.max = _browserFrames.length - 1;
          if (bar) bar.style.display = _browserFrames.length > 1 ? 'block' : 'none';
          // If user is NOT scrubbing, follow the latest frame
          if (!_browserScrubMode) {
            scrub.value = _browserFrames.length - 1;
            _browserShowFrameAt(_browserFrames.length - 1);
          }
          // If user IS scrubbing, just keep their position; the new frame is in the buffer
        }
        frameCount++;
        _browserSetStatus(`${frameCount} frame${frameCount===1?'':'s'} · buffer ${_browserFrames.length}`);
        _browserLastFrameTs = frame.ts;
      } catch (err) {
        console.error('Browser frame parse error', err);
      }
    };
  }

  function _browserStopStream() {
    if (_browserSSE) {
      _browserSSE.close();
      _browserSSE = null;
    }
  }

  function _browserDrawFrame(jpegB64) {
    const canvas = document.getElementById('browser-canvas');
    const empty  = document.getElementById('browser-empty');
    if (!canvas) return;
    const img = new Image();
    img.onload = () => {
      const containerWidth = canvas.parentElement.clientWidth;
      const ratio = img.height / img.width;
      canvas.width = img.width;
      canvas.height = img.height;
      canvas.style.width = containerWidth + 'px';
      canvas.style.height = (containerWidth * ratio) + 'px';
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      if (empty) empty.style.display = 'none';
      // Track natural dimensions for co-pilot click coordinate scaling
      _browserFrameW = img.width;
      _browserFrameH = img.height;
    };
    img.src = `data:image/jpeg;base64,${jpegB64}`;
  }

  // Phase 1.3 — Co-pilot click + type
  async function _browserSendClick(canvasX, canvasY) {
    const canvas = document.getElementById('browser-canvas');
    if (!canvas || !_browserFrameW) return;
    const rect = canvas.getBoundingClientRect();
    // Scale from rendered (CSS) pixels to natural frame pixels
    const scaleX = _browserFrameW / rect.width;
    const scaleY = _browserFrameH / rect.height;
    const pageX = Math.round(canvasX * scaleX);
    const pageY = Math.round(canvasY * scaleY);
    haptic();
    try {
      const r = await api('/api/browser/click', {
        method: 'POST', body: JSON.stringify({ x: pageX, y: pageY }),
      });
      if (r.error) toast(`click: ${r.error}`);
      else if (r.result) toast(`clicked: ${(r.result || '').slice(0, 60)}`);
    } catch { toast('click failed'); }
  }

  async function _browserSendType(text) {
    if (!text) return;
    haptic('medium');
    try {
      const r = await api('/api/browser/type', {
        method: 'POST', body: JSON.stringify({ text }),
      });
      if (r.error) toast(`type: ${r.error}`);
    } catch { toast('type failed'); }
  }

  async function _browserSendKey(key) {
    haptic();
    try {
      await api('/api/browser/key', {
        method: 'POST', body: JSON.stringify({ key }),
      });
    } catch { toast('key failed'); }
  }

  function _browserToggleCopilot() {
    _browserCopilotMode = !_browserCopilotMode;
    const btn = document.getElementById('browser-copilot-btn');
    const bar = document.getElementById('browser-copilot-bar');
    const canvas = document.getElementById('browser-canvas');
    if (_browserCopilotMode) {
      btn.textContent = '✓ Driving';
      btn.style.background = 'var(--tg-theme-button-color)';
      btn.style.color = 'white';
      bar.style.display = 'block';
      canvas.style.cursor = 'crosshair';
    } else {
      btn.textContent = '🖱 Drive';
      btn.style.background = '';
      btn.style.color = '';
      bar.style.display = 'none';
      canvas.style.cursor = '';
    }
    haptic('medium');
  }

  function _browserSetStatus(text) {
    const el = document.getElementById('browser-status');
    if (el) el.textContent = text;
  }

  async function renderShortcuts() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header" style="display:flex;align-items:center;gap:8px">
          <span class="nav-title" style="flex:1">⚡ Shortcuts</span>
          <button id="sc-autosend-toggle" class="btn btn-ghost"
                  style="width:auto;padding:4px 10px;font-size:11px"
                  title="Auto-send: tap a shortcut to fire instantly via Telethon. When off, opens chat input for manual review/edit before sending.">
            ${_scAutoSend() ? '⚡ Auto-send: ON' : '✏ Auto-send: OFF'}
          </button>
        </div>
        <div id="sc-cats"><div class="spinner" style="margin:24px auto"></div></div>
      </div>`;
    document.getElementById('sc-autosend-toggle').addEventListener('click', (e) => {
      haptic();
      const newState = !_scAutoSend();
      _scSetAutoSend(newState);
      e.target.textContent = newState ? '⚡ Auto-send: ON' : '✏ Auto-send: OFF';
      toast(newState
        ? 'Auto-send ON — taps fire instantly'
        : 'Auto-send OFF — taps open chat for manual send');
    });
    try {
      const items = await api('/api/shortcuts');
      const el = document.getElementById('sc-cats');
      if (!items.length) { el.innerHTML = `<div class="empty-state">No shortcuts configured</div>`; return; }
      // Group by category field; fallback to 'General'
      const groups = {};
      for (const s of items) {
        const cat = s.category || 'General';
        (groups[cat] = groups[cat] || []).push(s);
      }
      const catNames = Object.keys(groups);
      // Single category — skip landing, show list directly
      if (catNames.length === 1) {
        el.innerHTML = '';
        renderScList(el, groups[catNames[0]]);
        return;
      }
      el.innerHTML = catNames.map(cat => {
        const list    = groups[cat];
        const preview = list[0];
        return `<div class="card mem-cat-card section-gap" data-cat="${esc(cat)}" style="cursor:pointer;user-select:none">
          <div class="status-row" style="padding:2px 0">
            <span style="font-size:22px;margin-right:12px;flex-shrink:0">${preview.icon || '📌'}</span>
            <div style="flex:1;min-width:0">
              <div class="memory-subject">${esc(cat)}</div>
              <div class="memory-details" style="margin-top:2px">${esc(preview.label)}</div>
            </div>
            <span class="tag" style="margin:0 8px;flex-shrink:0">${list.length}</span>
            <span style="color:var(--tg-theme-hint-color);font-size:20px;flex-shrink:0">›</span>
          </div>
        </div>`;
      }).join('');
      el.querySelectorAll('.mem-cat-card').forEach(card => {
        card.onclick = () => {
          haptic();
          renderScCategory(card.dataset.cat, groups[card.dataset.cat]);
        };
      });
    } catch { document.getElementById('sc-cats').innerHTML = `<div class="empty-state">Failed to load</div>`; }
  }

  function renderScCategory(catName, items) {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <button class="btn btn-ghost" id="sc-back-btn" style="width:auto;padding:4px 10px">‹ Back</button>
          <span class="nav-title">⚡ ${esc(catName)}</span>
        </div>
        <div id="sc-list"></div>
      </div>`;
    document.getElementById('sc-back-btn').onclick = () => { haptic(); renderShortcuts(); };
    renderScList(document.getElementById('sc-list'), items);
  }

  function renderScList(container, items) {
    container.innerHTML = items.map((s, i) => `
      <div class="shortcut-card" data-idx="${i}">
        <span class="shortcut-icon">${s.icon || '📌'}</span>
        <div style="min-width:0">
          <div class="shortcut-label">${esc(s.label)}</div>
          <div class="shortcut-preview">${esc(s.prompt || '')}</div>
        </div>
      </div>`).join('');
    container.querySelectorAll('.shortcut-card').forEach(card => {
      const prompt = items[parseInt(card.dataset.idx, 10)].prompt;
      card.onclick = () => _scFire(prompt);
    });
  }

  // ── Settings ──────────────────────────────────────────────────────────────
  async function renderSettings() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">⚙️ Settings</span></div>
        <div id="settings-body"><div class="spinner" style="margin:24px auto"></div></div>
      </div>`;
    try {
      const [status, infer] = await Promise.all([api('/api/status'), api('/api/inference/status')]);
      renderSettingsBody(status, infer);
    } catch { document.getElementById('settings-body').innerHTML = `<div class="empty-state">Failed to load</div>`; }
  }

  function renderSettingsBody(status, infer) {
    const modelShort = (status.model || 'unknown').split('/').pop();
    const inferOn    = infer.active;
    document.getElementById('settings-body').innerHTML = `
      <div class="card">
        <div class="card-section-label">Model</div>
        <div class="settings-row" id="model-row">
          <div class="settings-row-label">${esc(modelShort)}</div>
          <span class="settings-row-arrow">›</span>
        </div>
      </div>
      <div class="card section-gap">
        <div class="card-section-label" style="display:flex;justify-content:space-between;align-items:center">
          <span>Inference</span>
          <button class="btn btn-ghost" id="infer-refresh-btn" style="width:auto;padding:2px 10px;font-size:14px">↻</button>
        </div>
        <div class="status-row">
          <div class="status-dot ${inferOn ? 'active' : 'inactive'}"></div>
          <div class="status-value">${inferOn ? 'Active' : 'Idle'}</div>
          ${infer.model ? `<div class="status-label" style="margin-left:auto">${esc(infer.model.split('/').pop())}</div>` : ''}
        </div>
        <div id="infer-loaded-list" style="font-size:11px;color:var(--tg-theme-hint-color);margin-top:6px;line-height:1.5">
          ${_renderLoadedModels(infer.loaded)}
        </div>
        <div class="status-row" id="infer-block-row" style="margin-top:10px">
          <div class="status-dot ${infer.blocked ? 'inactive' : 'active'}"></div>
          <div class="status-value">${infer.blocked ? 'Blocked' : 'Allowed'}</div>
          <button class="btn btn-ghost" id="infer-block-btn" style="width:auto;padding:2px 12px;margin-left:auto;font-size:13px">
            ${infer.blocked ? 'Unblock' : 'Block'}
          </button>
        </div>
        <button class="btn btn-secondary section-gap" id="infer-restart-btn">Restart Inference Bridge</button>
      </div>
      <div class="card section-gap">
        <div class="card-section-label">Stack</div>
        <div class="btn-row">
          <button class="btn btn-primary" id="stack-start-btn">Start</button>
          <button class="btn btn-danger"  id="stack-stop-btn">Stop</button>
        </div>
        <button class="btn btn-secondary section-gap" id="stack-restart-btn">Restart Stack</button>
      </div>
      <div class="card section-gap">
        <div class="card-section-label">OpenClaw</div>
        <div class="settings-row" id="oc-config-row">
          <div class="settings-row-label">Config</div>
          <span class="settings-row-arrow">›</span>
        </div>
        <div class="settings-row" id="oc-doctor-row">
          <div class="settings-row-label">Doctor</div>
          <span class="settings-row-arrow">›</span>
        </div>
      </div>
      <div class="card section-gap">
        <div class="card-section-label">Pending Pairings</div>
        <div id="pairing-list">
          <div style="font-size:13px;color:var(--tg-theme-hint-color);padding:6px 4px">Loading…</div>
        </div>
      </div>
      <div class="card section-gap">
        <div class="card-section-label" style="display:flex;justify-content:space-between;align-items:center">
          <span>Guest Usage (today)</span>
          <button class="btn btn-ghost" id="guest-usage-refresh-btn" style="width:auto;padding:2px 10px;font-size:14px">↻</button>
        </div>
        <div id="guest-usage-list">
          <div style="font-size:13px;color:var(--tg-theme-hint-color);padding:6px 4px">Loading…</div>
        </div>
      </div>
      <div class="card section-gap">
        <div class="card-section-label">Security</div>
        <div class="settings-row" id="sessions-row">
          <div class="settings-row-label">Active Sessions</div>
          <span class="settings-row-arrow">›</span>
        </div>
        <div class="settings-row" id="passkeys-row">
          <div class="settings-row-label">Passkeys</div>
          <span class="settings-row-arrow">›</span>
        </div>
        <div class="settings-row" id="logout-row" style="border-top:1px solid rgba(128,128,128,.15)">
          <div class="settings-row-label" style="color:#ff3b30">Log Out</div>
        </div>
      </div>
      <div style="text-align:center;padding:20px 0 8px;font-size:12px;color:var(--tg-theme-hint-color)">
        Sentinel &nbsp;<span id="settings-version">v—</span>
      </div>`;

    api('/api/version').then(d => {
      const el = document.getElementById('settings-version');
      if (el) el.textContent = `v${d.version}`;
    }).catch(() => {});
    document.getElementById('model-row').onclick         = () => { haptic(); push('model-select'); };
    document.getElementById('infer-restart-btn').onclick = restartInfer;
    document.getElementById('infer-refresh-btn').onclick = _refreshInferModels;
    document.getElementById('infer-block-btn').onclick   = toggleInferBlock;
    document.getElementById('stack-start-btn').onclick   = () => stackAction('start');
    document.getElementById('stack-stop-btn').onclick    = () => stackAction('stop');
    document.getElementById('stack-restart-btn').onclick = () => stackAction('restart');
    document.getElementById('oc-config-row').onclick     = () => { haptic(); push('openclaw-config'); };
    document.getElementById('oc-doctor-row').onclick     = () => { haptic(); push('openclaw-doctor'); };
    document.getElementById('sessions-row').onclick      = () => { haptic(); push('sessions'); };
    document.getElementById('passkeys-row').onclick      = () => { haptic(); push('passkeys'); };
    document.getElementById('logout-row').onclick        = () => { haptic(); _clearSession(); renderLogin(); };
    document.getElementById('guest-usage-refresh-btn').onclick = () => { haptic(); _loadGuestUsage(); };
    _loadPendingPairings();
    _loadGuestUsage();
  }

  async function _loadGuestUsage() {
    const el = document.getElementById('guest-usage-list');
    if (!el) return;
    let rows = [];
    try { rows = await api('/api/guests/usage'); } catch { rows = []; }
    if (!Array.isArray(rows) || !rows.length) {
      el.innerHTML = `<div style="font-size:13px;color:var(--tg-theme-hint-color);padding:6px 4px">No guests registered yet.</div>`;
      return;
    }
    el.innerHTML = rows.map(r => {
      const name = r.first_name || r.username || `Guest ${r.chat_id}`;
      const sub  = r.username ? `@${r.username} · ${r.chat_id}` : r.chat_id;
      const pct  = Math.min(100, Math.round((r.messages / Math.max(r.max_messages, 1)) * 100));
      const barColor = r.throttled ? '#ff3b30' : pct >= 80 ? '#ff9500' : '#4cd964';
      const badge = r.throttled
        ? `<span style="font-size:10px;color:#ff3b30;font-weight:600">THROTTLED</span>`
        : `<span style="font-size:11px;color:var(--tg-theme-hint-color)">${r.messages}/${r.max_messages}</span>`;
      return `<div class="guest-row" data-cid="${esc(r.chat_id)}" data-cap="${r.max_messages}">
        <div style="display:flex;align-items:center;gap:8px">
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis">${esc(name)}</div>
            <div style="font-size:11px;color:var(--tg-theme-hint-color);margin-top:1px">${esc(sub)}</div>
          </div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
            ${badge}
            <button class="btn btn-ghost guest-edit-btn" data-cid="${esc(r.chat_id)}" data-cap="${r.max_messages}"
                    style="width:auto;padding:2px 10px;font-size:11px">Edit cap</button>
          </div>
        </div>
        <div style="background:rgba(128,128,128,0.15);border-radius:3px;height:4px;margin:8px 0 0;overflow:hidden">
          <div style="background:${barColor};height:100%;width:${pct}%;transition:width .3s"></div>
        </div>
      </div>`;
    }).join('');
    el.querySelectorAll('.guest-edit-btn').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        _openGuestCapEditor(btn.dataset.cid, parseInt(btn.dataset.cap, 10));
      };
    });
  }

  function _openGuestCapEditor(chat_id, currentCap) {
    haptic();
    // Inline editor — avoids unreliable tg.showPopup / window.prompt in WebView
    const row = document.querySelector(`.guest-row[data-cid="${chat_id}"]`);
    if (!row) return;
    // Toggle: if already open, close
    const existing = row.querySelector('.guest-cap-editor');
    if (existing) { existing.remove(); return; }
    const editor = document.createElement('div');
    editor.className = 'guest-cap-editor';
    editor.style.cssText = 'margin-top:10px;padding:8px;background:rgba(128,128,128,0.08);border-radius:8px';
    editor.innerHTML = `
      <div style="font-size:12px;color:var(--tg-theme-hint-color);margin-bottom:6px">Daily cap (messages):</div>
      <div style="display:flex;gap:6px;align-items:center">
        <input class="tags-input" type="number" inputmode="numeric" min="1" max="10000" value="${currentCap}"
               style="flex:1;padding:6px 10px;font-size:14px" />
        <button class="btn btn-secondary cap-preset" data-v="20"  style="width:auto;padding:4px 10px;font-size:12px">20</button>
        <button class="btn btn-secondary cap-preset" data-v="50"  style="width:auto;padding:4px 10px;font-size:12px">50</button>
        <button class="btn btn-secondary cap-preset" data-v="100" style="width:auto;padding:4px 10px;font-size:12px">100</button>
      </div>
      <div class="btn-row" style="margin-top:6px">
        <button class="btn btn-secondary cap-cancel">Cancel</button>
        <button class="btn btn-primary   cap-save">Save</button>
      </div>`;
    row.appendChild(editor);
    const input = editor.querySelector('input');
    input.focus(); input.select();
    editor.querySelectorAll('.cap-preset').forEach(b => {
      b.onclick = (e) => { e.stopPropagation(); input.value = b.dataset.v; haptic(); };
    });
    editor.querySelector('.cap-cancel').onclick = (e) => { e.stopPropagation(); editor.remove(); };
    editor.querySelector('.cap-save').onclick = async (e) => {
      e.stopPropagation();
      const v = parseInt(input.value, 10);
      if (!v || v < 1) { toast('Invalid cap'); return; }
      try {
        await api('/api/guests/cap', { method: 'POST', body: JSON.stringify({ chat_id, max_messages: v }) });
        haptic('medium'); toast(`Cap → ${v}`);
        await _loadGuestUsage();
      } catch { toast('Failed to update cap'); }
    };
  }

  function _renderLoadedModels(loaded) {
    if (!Array.isArray(loaded) || !loaded.length) {
      return `<i>No models loaded — open LM Studio to load one.</i>`;
    }
    return `Loaded: ${loaded.map(m => esc(m.split('/').pop())).join(', ')}`;
  }

  async function _refreshInferModels() {
    const btn  = document.getElementById('infer-refresh-btn');
    const list = document.getElementById('infer-loaded-list');
    if (!btn || !list) return;
    haptic();
    btn.textContent = '…'; btn.disabled = true;
    try {
      const infer = await api('/api/inference/status?force=1');
      list.innerHTML = _renderLoadedModels(infer.loaded);
      // Update the active-model badge if it changed
      const badge = document.querySelector('.card-section-label + .status-row .status-label');
      if (badge && infer.model) badge.textContent = infer.model.split('/').pop();
      // Sync block-state row (watchdog may have flipped it via Steam)
      const row = document.getElementById('infer-block-row');
      const blockBtn = document.getElementById('infer-block-btn');
      if (row && blockBtn) {
        const blocked = !!infer.blocked;
        blockBtn.textContent = blocked ? 'Unblock' : 'Block';
        row.querySelector('.status-dot').className = `status-dot ${blocked ? 'inactive' : 'active'}`;
        row.querySelector('.status-value').textContent = blocked ? 'Blocked' : 'Allowed';
      }
      toast(`${(infer.loaded || []).length} model(s) loaded`);
    } catch {
      toast('Refresh failed');
    } finally {
      btn.textContent = '↻'; btn.disabled = false;
    }
  }

  async function _loadPendingPairings() {
    const el = document.getElementById('pairing-list');
    if (!el) return;
    let pending = [];
    try { pending = await api('/api/pairing/pending'); } catch { pending = []; }
    if (!Array.isArray(pending) || !pending.length) {
      el.innerHTML = `<div style="font-size:13px;color:var(--tg-theme-hint-color);padding:6px 4px">No pending pairings.</div>`;
      return;
    }
    el.innerHTML = pending.map(p => {
      const name = p.first_name || p.username || p.chat_id;
      const sub  = p.username ? `@${p.username} · ${p.chat_id}` : `ID ${p.chat_id}`;
      return `<div class="pairing-row" data-code="${esc(p.code)}" data-cid="${esc(p.chat_id)}" data-name="${esc(name)}">
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:14px">${esc(name)}</div>
          <div style="font-size:11px;color:var(--tg-theme-hint-color);margin-top:2px">${esc(sub)}</div>
        </div>
        <button class="btn btn-primary pairing-approve-btn" style="width:auto;padding:6px 14px;font-size:12px;margin-left:8px">Approve</button>
      </div>`;
    }).join('');
    el.querySelectorAll('.pairing-row').forEach(row => {
      row.querySelector('.pairing-approve-btn').onclick = () => _approvePairing(row);
    });
  }

  async function _approvePairing(row) {
    const code = row.dataset.code;
    const name = row.dataset.name;
    const btn  = row.querySelector('.pairing-approve-btn');
    haptic('medium');
    btn.textContent = '…'; btn.disabled = true;
    try {
      const result = await api('/api/pairing/approve', {
        method: 'POST', body: JSON.stringify({ code }),
      });
      if (result.ok) {
        toast(`✅ Approved ${name}`);
        await _loadPendingPairings();
      } else {
        toast(`Failed: ${result.stderr || result.error || 'unknown'}`);
        btn.textContent = 'Approve'; btn.disabled = false;
      }
    } catch {
      toast('Approve failed');
      btn.textContent = 'Approve'; btn.disabled = false;
    }
  }

  async function restartInfer() {
    haptic();
    const btn = document.getElementById('infer-restart-btn');
    btn.textContent = 'Restarting…'; btn.disabled = true;
    try { await api('/api/inference/restart', { method: 'POST' }); toast('Inference bridge restarting…'); }
    catch { toast('Failed to restart'); }
    btn.textContent = 'Restart Inference Bridge'; btn.disabled = false;
  }

  async function toggleInferBlock() {
    haptic();
    const btn = document.getElementById('infer-block-btn');
    const row = document.getElementById('infer-block-row');
    if (!btn || !row) return;
    const currentlyBlocked = btn.textContent.trim() === 'Unblock';
    const next = !currentlyBlocked;
    btn.disabled = true;
    try {
      const r = await api('/api/inference/block', {
        method: 'POST', body: JSON.stringify({ blocked: next }),
      });
      const blocked = !!r.blocked;
      btn.textContent = blocked ? 'Unblock' : 'Block';
      row.querySelector('.status-dot').className = `status-dot ${blocked ? 'inactive' : 'active'}`;
      row.querySelector('.status-value').textContent = blocked ? 'Blocked' : 'Allowed';
      toast(blocked ? 'Inference blocked' : 'Inference unblocked');
    } catch {
      toast('Toggle failed');
    } finally {
      btn.disabled = false;
    }
  }

  async function stackAction(action) {
    haptic('medium');
    const btn = document.getElementById(`stack-${action}-btn`);
    if (btn) btn.disabled = true;
    try { await api(`/api/stack/${action}`, { method: 'POST' }); toast({ start: 'Starting…', stop: 'Stopping…', restart: 'Restarting…' }[action] || 'Done'); }
    catch { toast('Failed'); }
    if (btn) btn.disabled = false;
  }

  // ── Model Select ──────────────────────────────────────────────────────────
  let _orFormOpen = false;

  async function renderModelSelect() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">Select Model</span>
          <button class="btn btn-ghost" id="or-add-btn" style="width:auto;padding:4px 10px">+ OpenRouter</button>
        </div>
        <div id="or-form-wrap"></div>
        <div class="card" id="model-list"><div class="spinner" style="margin:24px auto"></div></div>
      </div>`;
    document.getElementById('or-add-btn').onclick = () => { haptic(); _toggleOrForm(); };
    await _loadModels();
  }

  async function _loadModels() {
    try {
      const data   = await api('/api/models');
      const models = data.models || [];
      const list   = document.getElementById('model-list');
      if (!models.length) { list.innerHTML = `<div class="empty-state">No models configured</div>`; return; }
      list.innerHTML = models.map(m => `
        <div class="model-item" data-id="${esc(m.id)}" data-provider="${esc(m.provider)}">
          <div>
            <div class="model-name">${esc(m.name)}</div>
            <div class="model-provider">${esc(m.provider)}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            ${m.active ? '<span class="model-check">✓</span>' : ''}
            ${m.provider === 'openrouter' ? `<button class="btn btn-ghost or-del-btn" data-id="${esc(m.id)}" style="width:auto;padding:2px 8px;font-size:14px;color:#ff3b30">×</button>` : ''}
          </div>
        </div>`).join('');
      list.querySelectorAll('.model-item').forEach(item => {
        item.onclick = (e) => {
          if (e.target.classList.contains('or-del-btn')) return;
          switchModel(item.dataset.id);
        };
      });
      list.querySelectorAll('.or-del-btn').forEach(btn => {
        btn.onclick = (e) => { e.stopPropagation(); _removeModel(btn.dataset.id); };
      });
      // Stash for the form
      window._orHasKey   = !!data.has_openrouter_key;
      window._orPresets  = data.openrouter_presets || [];
    } catch { document.getElementById('model-list').innerHTML = `<div class="empty-state">Failed to load models</div>`; }
  }

  function _toggleOrForm() {
    _orFormOpen = !_orFormOpen;
    const wrap = document.getElementById('or-form-wrap');
    if (!_orFormOpen) { wrap.innerHTML = ''; return; }
    const presets = window._orPresets || [];
    const hasKey  = !!window._orHasKey;
    const opts    = presets.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    wrap.innerHTML = `
      <div class="card section-gap">
        <div class="card-section-label">Add OpenRouter Model</div>
        <div class="add-memory-form">
          <select id="or-model-select" class="tags-input" style="margin-top:6px">${opts}</select>
          <input class="tags-input" id="or-name" placeholder="Display name (optional)" />
          <input class="tags-input" id="or-key" type="password"
                 placeholder="${hasKey ? 'API key on file — leave blank to reuse' : 'OpenRouter API key (sk-or-…)'}" />
          <div style="font-size:11px;color:var(--tg-theme-hint-color);margin-top:-4px">
            Key is stored in Windows Credential Manager and reused for all OpenRouter models.
          </div>
          <div class="btn-row section-gap">
            <button class="btn btn-secondary" id="or-cancel">Cancel</button>
            <button class="btn btn-primary"   id="or-save">Add Model</button>
          </div>
        </div>
      </div>`;
    document.getElementById('or-cancel').onclick = () => { haptic(); _orFormOpen = false; wrap.innerHTML = ''; };
    document.getElementById('or-save').onclick   = _addOpenRouterModel;
    // Auto-fill display name when preset changes
    const sel = document.getElementById('or-model-select');
    const nm  = document.getElementById('or-name');
    const fillName = () => {
      const preset = presets.find(p => p.id === sel.value);
      if (preset && !nm.value) nm.placeholder = `Display name (default: ${preset.name})`;
    };
    sel.onchange = fillName; fillName();
  }

  async function _addOpenRouterModel() {
    const model_id = document.getElementById('or-model-select')?.value?.trim();
    const name     = document.getElementById('or-name')?.value?.trim();
    const api_key  = document.getElementById('or-key')?.value?.trim();
    if (!model_id) { toast('Pick a model'); return; }
    if (!window._orHasKey && !api_key) { toast('API key required first time'); return; }
    const btn = document.getElementById('or-save');
    btn.textContent = 'Adding…'; btn.disabled = true;
    try {
      const result = await api('/api/models/openrouter/add', {
        method: 'POST',
        body: JSON.stringify({ model_id, name: name || undefined, api_key: api_key || undefined }),
      });
      if (result.error) { toast(`Error: ${result.error}`); btn.textContent = 'Add Model'; btn.disabled = false; return; }
      haptic('medium'); toast('Model added');
      _orFormOpen = false;
      document.getElementById('or-form-wrap').innerHTML = '';
      await _loadModels();
    } catch { toast('Failed to add'); btn.textContent = 'Add Model'; btn.disabled = false; }
  }

  async function _removeModel(modelId) {
    haptic('medium');
    const go = async () => {
      try {
        await api(`/api/models/${encodeURIComponent(modelId)}`, { method: 'DELETE' });
        toast('Removed'); await _loadModels();
      } catch { toast('Failed to remove'); }
    };
    if (tg?.showConfirm) {
      tg.showConfirm(`Remove ${modelId}?`, ok => { if (ok) go(); });
    } else {
      if (window.confirm(`Remove ${modelId}?`)) go();
    }
  }

  async function switchModel(modelId) {
    haptic('medium');
    try { await api('/api/models/active', { method: 'POST', body: JSON.stringify({ model_id: modelId }) }); toast('Model switched — gateway will reload'); pop(); }
    catch { toast('Failed to switch model'); }
  }

  // ── Sessions ──────────────────────────────────────────────────────────────
  async function renderSessions() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">🔐 Active Sessions</span></div>
        <div id="sessions-list"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    loadSessions();
  }

  async function loadSessions() {
    const el = document.getElementById('sessions-list');
    if (!el) return;
    try {
      const sessions = await api('/api/auth/sessions');
      const myTok    = _getSession()?.token || '';
      if (!sessions.length) { el.innerHTML = `<div class="card"><div class="empty-state">No active sessions</div></div>`; return; }
      el.innerHTML = `<div class="card">${sessions.map(s => {
        const isCurrent = s.token === myTok;
        const ua      = _parseUA(s.ua);
        const created = new Date(s.created_at * 1000).toLocaleString('en-SG', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        const expires = new Date(s.expires_at * 1000).toLocaleString('en-SG', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        return `<div class="session-item">
          <div class="session-header">
            <span class="session-icon">${ua.icon}</span>
            <div style="flex:1;min-width:0">
              <div class="session-device">${esc(ua.label)}${isCurrent ? ' <span class="session-current">current</span>' : ''}</div>
              <div class="session-meta">${esc(s.ip || 'unknown IP')} · signed in ${created}</div>
              <div class="session-meta">expires ${expires}</div>
            </div>
            ${isCurrent
              ? `<span class="session-current-dot"></span>`
              : `<button class="memory-delete session-revoke" data-token="${esc(s.id)}" title="Revoke">×</button>`}
          </div>
        </div>`;
      }).join('')}</div>
      <button class="btn btn-danger section-gap" id="revoke-others-btn">Revoke All Other Sessions</button>`;

      el.querySelectorAll('.session-revoke').forEach(btn => {
        btn.onclick = () => revokeSession(btn.dataset.token, false);
      });
      document.getElementById('revoke-others-btn').onclick = () => revokeAllOthers(myTok, sessions);
    } catch { if (el) el.innerHTML = `<div class="card"><div class="empty-state">Failed to load sessions</div></div>`; }
  }

  function _parseUA(ua = '') {
    const u = ua.toLowerCase();
    if (u.includes('iphone') || u.includes('ipad')) return { icon: '📱', label: 'iPhone / iPad' };
    if (u.includes('android'))   return { icon: '📱', label: 'Android' };
    if (u.includes('mac'))       return { icon: '💻', label: 'Mac' };
    if (u.includes('windows'))   return { icon: '🖥',  label: 'Windows' };
    if (u.includes('linux'))     return { icon: '🐧', label: 'Linux' };
    if (u.includes('curl') || u.includes('python')) return { icon: '⚙️', label: 'API client' };
    return { icon: '🌐', label: 'Browser' };
  }

  async function revokeSession(tokenId, isSelf) {
    haptic('medium');
    try {
      await api(`/api/auth/sessions/${tokenId}`, { method: 'DELETE' });
      if (isSelf) { _clearSession(); renderLogin(); return; }
      toast('Session revoked'); loadSessions();
    } catch { toast('Failed to revoke'); }
  }

  async function revokeAllOthers(myTok, sessions) {
    haptic('medium');
    const others = sessions.filter(s => s.token !== myTok);
    if (!others.length) { toast('No other sessions'); return; }
    await Promise.all(others.map(s => api(`/api/auth/sessions/${s.id}`, { method: 'DELETE' }).catch(() => {})));
    toast(`Revoked ${others.length} session${others.length > 1 ? 's' : ''}`);
    loadSessions();
  }

  // ── OpenClaw Config ───────────────────────────────────────────────────────
  async function renderOpenClawConfig() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">🧠 OpenClaw Config</span></div>
        <div id="oc-config-body"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    try {
      const cfg = await api('/api/openclaw/config');
      const el  = document.getElementById('oc-config-body');

      let selEffort = cfg.reasoning_effort;
      let selMaxTok = cfg.max_tokens;
      const effortLabels = { none: 'Off', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Max' };
      const tokOptions   = [2048, 4096, 8192, 16384];
      const tokLabels    = { 2048: '2k', 4096: '4k', 8192: '8k', 16384: '16k' };

      el.innerHTML = `
        <div class="card">
          <div class="card-section-label">Model</div>
          <div class="settings-row" style="cursor:default;padding:10px 0">
            <div class="settings-row-label">${esc(cfg.model_name)}</div>
            <span style="font-size:12px;color:var(--tg-theme-hint-color)">active</span>
          </div>
        </div>
        <div class="card section-gap">
          <div class="card-section-label">Reasoning Effort</div>
          <div class="effort-pills" id="effort-pills" style="margin:10px 0 4px">
            ${cfg.available_efforts.map(e => `
              <button class="effort-pill${selEffort === e ? ' active' : ''}" data-val="${esc(e)}" data-group="effort">
                ${esc(effortLabels[e] || e)}
              </button>`).join('')}
          </div>
        </div>
        <div class="card section-gap">
          <div class="card-section-label">Max Response Tokens</div>
          <div class="effort-pills" id="maxtok-pills" style="margin:10px 0 4px">
            ${tokOptions.map(t => `
              <button class="effort-pill${selMaxTok === t ? ' active' : ''}" data-val="${t}" data-group="maxtok">
                ${tokLabels[t]}
              </button>`).join('')}
          </div>
        </div>
        <div class="card section-gap">
          <div class="card-section-label keys-card-header" id="keys-header">
            <span>Keys</span>
            <span class="keys-card-arrow" id="keys-arrow">›</span>
          </div>
          <div id="keys-list" hidden><div class="skill-creds-loading">Loading…</div></div>
        </div>
        <div class="card section-gap">
          <div class="card-section-label">Connection</div>
          <div class="settings-row" style="cursor:default">
            <div class="settings-row-label">Timeout</div>
            <div style="display:flex;align-items:center;gap:6px">
              <input type="number" id="timeout-input" value="${cfg.timeout_seconds}"
                     min="30" max="3600" step="30" class="oc-number-input">
              <span style="font-size:12px;color:var(--tg-theme-hint-color)">sec</span>
            </div>
          </div>
        </div>
        <div class="card section-gap">
          <div class="card-section-label">Web</div>
          <div class="settings-row" style="cursor:default">
            <div class="settings-row-label">Search</div>
            <label class="toggle"><input type="checkbox" id="web-search-tog" ${cfg.web_search ? 'checked' : ''}><span class="toggle-slider"></span></label>
          </div>
          <div class="settings-row" style="cursor:default">
            <div class="settings-row-label">Fetch</div>
            <label class="toggle"><input type="checkbox" id="web-fetch-tog" ${cfg.web_fetch ? 'checked' : ''}><span class="toggle-slider"></span></label>
          </div>
        </div>
        <div class="card section-gap">
          <div class="card-section-label">Skills</div>
          <div class="settings-row" id="oc-skills-row">
            <div class="settings-row-label">Manage Skills</div>
            <span class="settings-row-arrow">›</span>
          </div>
        </div>
        <button class="btn btn-primary section-gap" id="oc-save-btn">Save</button>`;

      el.querySelectorAll('#effort-pills .effort-pill').forEach(btn => {
        btn.onclick = () => {
          haptic(); selEffort = btn.dataset.val;
          el.querySelectorAll('#effort-pills .effort-pill').forEach(b => b.classList.toggle('active', b.dataset.val === selEffort));
        };
      });
      el.querySelectorAll('#maxtok-pills .effort-pill').forEach(btn => {
        btn.onclick = () => {
          haptic(); selMaxTok = parseInt(btn.dataset.val);
          el.querySelectorAll('#maxtok-pills .effort-pill').forEach(b => b.classList.toggle('active', b.dataset.val === String(selMaxTok)));
        };
      });

      document.getElementById('oc-skills-row').onclick = () => { haptic(); push('openclaw-skills'); };

      // Keys card — collapsible. Defer load until first expand.
      let keysLoaded = false;
      document.getElementById('keys-header').onclick = () => {
        haptic();
        const list  = document.getElementById('keys-list');
        const arrow = document.getElementById('keys-arrow');
        const isOpen = !list.hidden;
        list.hidden = isOpen;
        arrow.textContent = isOpen ? '›' : '⌄';
        if (!isOpen && !keysLoaded) {
          keysLoaded = true;
          _loadKeysList();
        }
      };

      document.getElementById('oc-save-btn').onclick = async () => {
        haptic('medium');
        const btn     = document.getElementById('oc-save-btn');
        btn.textContent = 'Saving…'; btn.disabled = true;
        const timeout = parseInt(document.getElementById('timeout-input').value) || cfg.timeout_seconds;
        try {
          await api('/api/openclaw/config', {
            method: 'POST',
            body: JSON.stringify({
              reasoning_effort: selEffort,
              max_tokens:       selMaxTok,
              timeout_seconds:  timeout,
              web_search:       document.getElementById('web-search-tog').checked,
              web_fetch:        document.getElementById('web-fetch-tog').checked,
            }),
          });
          toast('Config saved — gateway will reload');
          pop();
        } catch {
          toast('Failed to save config');
          btn.textContent = 'Save'; btn.disabled = false;
        }
      };
    } catch {
      document.getElementById('oc-config-body').innerHTML =
        `<div class="card"><div class="empty-state">Failed to load config</div></div>`;
    }
  }

  // ── Tool Drawer ───────────────────────────────────────────────────────────
  // Two-level: server list -> per-server tool toggles. State persists in
  // MetaMCP's namespace_tool_mappings table. Toggles only take effect on the
  // next /new (LLM tool catalog is snapshotted at session start).

  async function renderToolDrawer() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">🧰 Tool Drawer</span></div>
        <div class="card section-gap" style="font-size:12px;color:var(--tg-theme-hint-color);line-height:1.4">
          ⓘ Toggle state persists in MetaMCP's DB. Changes only reach the
          bot after you run <b>/new</b> — OpenClaw snapshots tools at
          session start.
        </div>
        <div id="td-body"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    try {
      const data = await api('/api/tools/servers');
      const body = document.getElementById('td-body');
      if (!data.servers || data.servers.length === 0) {
        body.innerHTML = '<div class="card"><div style="padding:12px;color:var(--tg-theme-hint-color)">No MCP servers mapped to the Default namespace.</div></div>';
        return;
      }
      body.innerHTML = data.servers.map(s => {
        const total = s.tools_total || 0;
        const disabled = s.tools_disabled || 0;
        const enabled = total - disabled;
        const allDisabled = (total > 0 && enabled === 0);
        const partial    = (disabled > 0 && enabled > 0);
        const dot = allDisabled ? 'inactive' : (partial ? 'partial' : 'active');
        const sub = total === 0
          ? '<i>tools not yet discovered — query the server once via the bot</i>'
          : (partial
              ? `${enabled} enabled · <span style="color:#ff9500">${disabled} off</span>`
              : (allDisabled ? `<span style="color:#ff3b30">all ${total} disabled</span>` : `${total} tools`));
        return `
          <div class="card section-gap td-server-row" data-name="${esc(s.name)}" style="cursor:pointer">
            <div class="status-row">
              <div class="status-dot ${dot}"></div>
              <div class="status-value">${esc(s.name)}</div>
              <span class="settings-row-arrow" style="margin-left:auto">›</span>
            </div>
            <div style="font-size:11px;color:var(--tg-theme-hint-color);margin-top:4px">${sub}</div>
          </div>`;
      }).join('');
      body.querySelectorAll('.td-server-row').forEach(el => {
        el.onclick = () => { haptic(); push('tool-drawer-server', { name: el.dataset.name }); };
      });
    } catch (e) {
      document.getElementById('td-body').innerHTML =
        `<div class="card"><div style="padding:12px;color:#ff3b30">Failed to load servers: ${esc(String(e))}</div></div>`;
    }
  }

  async function renderToolDrawerServer({ name }) {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">🧰 ${esc(name)}</span></div>
        <div class="card section-gap" style="font-size:12px;color:var(--tg-theme-hint-color);line-height:1.4">
          ⓘ Disabling hides this tool from the bot's tool catalogue on the
          next <b>/new</b>. Re-enable to restore.
        </div>
        <div id="td-tools"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    try {
      const data = await api(`/api/tools/server/${encodeURIComponent(name)}/tools`);
      const body = document.getElementById('td-tools');
      if (!data.tools || data.tools.length === 0) {
        body.innerHTML = '<div class="card"><div style="padding:12px;color:var(--tg-theme-hint-color)">No tools discovered for this server yet. Hit the server once via the bot and come back.</div></div>';
        return;
      }
      body.innerHTML = `<div class="card section-gap">${data.tools.map(t => {
        const enabled = (t.status === 'ACTIVE');
        const desc = (t.description || '').replace(/\s+/g, ' ').slice(0, 140);
        return `
          <div class="settings-row td-tool-row" data-uuid="${esc(t.tool_uuid)}" data-enabled="${enabled ? '1' : '0'}" style="align-items:flex-start;padding:10px 4px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:14px;font-family:ui-monospace,Menlo,monospace">${esc(t.tool_name)}</div>
              <div style="font-size:11px;color:var(--tg-theme-hint-color);margin-top:3px;line-height:1.4">${esc(desc)}${desc.length === 140 ? '…' : ''}</div>
            </div>
            <label class="td-switch" style="margin-left:12px;flex-shrink:0">
              <input type="checkbox" ${enabled ? 'checked' : ''}>
              <span class="td-slider"></span>
            </label>
          </div>`;
      }).join('')}</div>`;
      body.querySelectorAll('.td-tool-row').forEach(row => {
        const cb = row.querySelector('input[type=checkbox]');
        cb.onchange = async () => {
          const uuid = row.dataset.uuid;
          const next = cb.checked;
          cb.disabled = true;
          try {
            await api('/api/tools/toggle', {
              method: 'POST',
              body: JSON.stringify({ tool_uuid: uuid, enabled: next }),
            });
            row.dataset.enabled = next ? '1' : '0';
            toast(next ? 'Enabled — run /new to apply' : 'Disabled — run /new to apply');
          } catch (e) {
            cb.checked = !next; // revert
            toast('Toggle failed');
          } finally {
            cb.disabled = false;
          }
        };
      });
    } catch (e) {
      document.getElementById('td-tools').innerHTML =
        `<div class="card"><div style="padding:12px;color:#ff3b30">Failed: ${esc(String(e))}</div></div>`;
    }
  }


  // ── Watchdog Control (v2) ────────────────────────────────────────────────
  // Pillar-aware view backed by sentinel-watchdog v2 API (:8200).
  // System metrics bar at top. Pillar accordions expand to per-service rows.
  // Restart buttons call v2 endpoints (dispatch_one / dispatch_pillar).
  // Logs button still uses v1 docker tail path (v2 logs endpoint in Phase D).

  async function renderWatchdogControl() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">🛡 Watchdog Control</span>
          <button class="btn btn-ghost" id="wc-refresh" style="width:auto;padding:2px 10px;font-size:14px">↻</button>
        </div>
        <div id="wc-system"></div>
        <div id="wc-body"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;

    document.getElementById('wc-refresh').onclick = () => { haptic(); renderWatchdogControl(); };

    // Fetch system metrics and pillar list in parallel
    const [sysRes, pillarsRes] = await Promise.allSettled([
      api('/api/v2/system'),
      api('/api/v2/pillars'),
    ]);

    // ── System metrics bar ──────────────────────────────────────────────
    const sysEl = document.getElementById('wc-system');
    if (sysRes.status === 'fulfilled' && !sysRes.value.error) {
      const s   = sysRes.value;
      const ram = s.ram  || {};
      const cpu = s.cpu  || {};
      const disks = (s.disks || []);
      const gpu = (s.gpus || [])[0] || null;

      const ramPct  = Math.round(ram.percent  || 0);
      const cpuPct  = Math.round(cpu.percent  || 0);
      const mainDisk = disks.find(d => (d.mountpoint || '').toUpperCase().startsWith('C')) || disks[0] || {};
      const diskPct = Math.round(mainDisk.percent || 0);
      const vramPct = gpu ? Math.round(gpu.vram_percent || 0) : null;

      const chip = (label, pct, warn, crit) => {
        const col = pct >= crit ? '#ff3b30' : pct >= warn ? '#ff9500' : '#34c759';
        return `<div style="display:flex;flex-direction:column;align-items:center;gap:2px;flex:1">
          <div style="font-size:14px;font-weight:700;color:${col}">${pct}%</div>
          <div style="font-size:10px;color:var(--tg-theme-hint-color)">${label}</div>
        </div>`;
      };

      const chips = [
        chip('RAM',  ramPct,  85, 95),
        chip('CPU',  cpuPct,  80, 95),
        diskPct  ? chip('Disk', diskPct, 90, 97)  : null,
        vramPct !== null ? chip('VRAM', vramPct, 85, 95) : null,
      ].filter(Boolean);

      sysEl.innerHTML = `
        <div class="card section-gap" style="display:flex;justify-content:space-around;padding:10px 8px">
          ${chips.join('')}
        </div>`;
    }

    // ── Pillar list ────────────────────────────────────────────────────
    const body = document.getElementById('wc-body');

    if (pillarsRes.status !== 'fulfilled' || pillarsRes.value.error) {
      const err = pillarsRes.reason || (pillarsRes.value || {}).error || 'unknown';
      body.innerHTML = `
        <div class="card">
          <div style="padding:12px;color:#ff3b30;font-size:13px">
            ⚠ Watchdog v2 unreachable (:8200).<br>
            <span style="color:var(--tg-theme-hint-color);font-size:11px">${esc(String(err).slice(0,120))}</span>
          </div>
        </div>`;
      return;
    }

    const pillars = pillarsRes.value.pillars || [];
    if (!pillars.length) {
      body.innerHTML = `<div class="card"><div style="padding:12px;color:var(--tg-theme-hint-color)">No pillars found.</div></div>`;
      return;
    }

    const _hdot = h => h === 'up' ? 'active' : h === 'warn' ? 'partial' : h === 'down' ? 'inactive' : 'inactive';
    const _sdot = s => s === 'up'  ? 'active' : 'inactive';

    body.innerHTML = pillars.map(p => `
      <div class="card section-gap" id="pc-${esc(p.id)}">
        <div class="pillar-hdr settings-row" data-pid="${esc(p.id)}"
             style="cursor:pointer;padding:10px 4px;user-select:none">
          <div style="flex:1;min-width:0">
            <div class="status-row" style="margin-bottom:2px">
              <div class="status-dot ${_hdot(p.health)}"></div>
              <div class="status-value" style="font-weight:600">${esc(p.emoji)} ${esc(p.name)}</div>
            </div>
            <div style="font-size:11px;color:var(--tg-theme-hint-color);margin-left:18px">
              ${p.up}↑&nbsp;${p.down}↓&nbsp;${p.unknown}?&nbsp;·&nbsp;${p.service_count} services
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
            <button class="btn btn-secondary pillar-rst" data-pid="${esc(p.id)}" data-pname="${esc(p.name)}"
                    style="width:auto;padding:4px 10px;font-size:12px">↺ All</button>
            <span class="pillar-chev" style="font-size:20px;line-height:1;color:var(--tg-theme-hint-color);transition:transform .2s">›</span>
          </div>
        </div>
        <div class="pillar-svcs" id="ps-${esc(p.id)}" style="display:none"></div>
      </div>`).join('');

    // ── Pillar accordion ───────────────────────────────────────────────
    body.querySelectorAll('.pillar-hdr').forEach(hdr => {
      hdr.onclick = async e => {
        if (e.target.classList.contains('pillar-rst')) return;
        haptic();
        const pid  = hdr.dataset.pid;
        const svc  = document.getElementById(`ps-${pid}`);
        const chev = hdr.querySelector('.pillar-chev');
        const open = svc.style.display !== 'none';
        if (open) {
          svc.style.display = 'none';
          chev.style.transform = '';
        } else {
          svc.style.display = 'block';
          chev.style.transform = 'rotate(90deg)';
          if (!svc.dataset.loaded) {
            svc.dataset.loaded = '1';
            svc.innerHTML = `<div style="padding:8px 4px;color:var(--tg-theme-hint-color);font-size:12px">Loading…</div>`;
            try {
              const d = await api(`/api/v2/pillars/${encodeURIComponent(pid)}`);
              const svcs = d.services || [];
              if (!svcs.length) {
                svc.innerHTML = `<div style="padding:8px 4px;color:var(--tg-theme-hint-color);font-size:12px">No services configured.</div>`;
                return;
              }
              svc.innerHTML = `
                <div style="border-top:1px solid var(--tg-theme-section-separator-color);margin:0 -12px;padding:4px 12px 0">
                  ${svcs.map(sv => {
                    const label = sv.id.replace(/-/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
                    const portStr = sv.port ? `:${sv.port}` : (sv.host || '—');
                    const hasCont = !!(sv.container);
                    return `
                      <div class="settings-row" style="align-items:center;padding:8px 0">
                        <div style="flex:1;min-width:0">
                          <div class="status-row" style="margin-bottom:1px">
                            <div class="status-dot ${_sdot(sv.state)}" style="width:7px;height:7px"></div>
                            <div style="font-size:13px;margin-left:6px">${esc(label)}</div>
                          </div>
                          <div style="font-size:10px;color:var(--tg-theme-hint-color);margin-left:13px">${esc(portStr)}</div>
                        </div>
                        <div style="display:flex;gap:4px;flex-shrink:0">
                          ${hasCont ? `<button class="btn btn-ghost svc-logs" data-container="${esc(sv.container)}" data-label="${esc(label)}" style="width:auto;padding:3px 8px;font-size:11px">📋</button>` : ''}
                          <button class="btn btn-ghost svc-rst" data-svc="${esc(sv.id)}" data-label="${esc(label)}"
                                  style="width:auto;padding:3px 8px;font-size:11px">↺</button>
                        </div>
                      </div>`;
                  }).join('')}
                </div>`;
              // Wire logs buttons
              svc.querySelectorAll('.svc-logs').forEach(btn => {
                btn.onclick = () => {
                  haptic();
                  push('watchdog-logs', { container: btn.dataset.container, label: btn.dataset.label });
                };
              });
              // Wire per-service restart buttons
              svc.querySelectorAll('.svc-rst').forEach(btn => {
                btn.onclick = async () => {
                  if (!confirm(`Restart "${btn.dataset.label}"?`)) return;
                  haptic('medium');
                  const orig = btn.textContent;
                  btn.textContent = '…'; btn.disabled = true;
                  try {
                    const r = await api(`/api/v2/services/${encodeURIComponent(btn.dataset.svc)}/restart`, { method: 'POST' });
                    if (r.method === 'external') {
                      toast(`${btn.dataset.label}: manual restart required`);
                    } else {
                      toast(r.ok
                        ? `${btn.dataset.label}: restarted ✓`
                        : `${btn.dataset.label}: failed — ${(r.detail || '').slice(0, 60)}`);
                    }
                  } catch (err) {
                    toast(`Restart failed: ${String(err).slice(0,60)}`);
                  } finally {
                    btn.textContent = orig; btn.disabled = false;
                    // Refresh service states in this pillar after restart
                    setTimeout(renderWatchdogControl, 3000);
                  }
                };
              });
            } catch (err) {
              svc.innerHTML = `<div style="padding:8px 4px;color:#ff3b30;font-size:12px">Failed: ${esc(String(err))}</div>`;
            }
          }
        }
      };
    });

    // ── Pillar-level restart buttons ───────────────────────────────────
    body.querySelectorAll('.pillar-rst').forEach(btn => {
      btn.onclick = async e => {
        e.stopPropagation();
        const pid   = btn.dataset.pid;
        const pname = btn.dataset.pname;
        if (!confirm(`Restart all services in "${pname}"?`)) return;
        haptic('medium');
        btn.textContent = '…'; btn.disabled = true;
        try {
          const r = await api(`/api/v2/pillars/${encodeURIComponent(pid)}/restart`, { method: 'POST' });
          toast(r.ok ? `${pname}: restart dispatched ✓` : `${pname}: some services failed`);
        } catch (err) {
          toast(`Restart failed: ${String(err).slice(0,60)}`);
        } finally {
          btn.textContent = '↺ All'; btn.disabled = false;
          setTimeout(renderWatchdogControl, 3000);
        }
      };
    });
  }

  async function renderWatchdogLogs({ container, label }) {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">📋 ${esc(label || container)}</span>
          <button class="btn btn-ghost" id="wl-refresh" style="width:auto;padding:2px 10px;font-size:14px">↻</button>
        </div>
        <div class="card section-gap" style="font-size:11px;color:var(--tg-theme-hint-color);line-height:1.4">
          ⓘ docker logs --tail 100 for <code>${esc(container)}</code>.
        </div>
        <div id="wl-body"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    const load = async () => {
      try {
        const r = await api('/api/services/logs', {
          method: 'POST',
          body: JSON.stringify({ container, lines: 100 }),
        });
        const body = document.getElementById('wl-body');
        if (!r.ok) {
          body.innerHTML = `<div class="card"><div style="padding:12px;color:#ff9500">${esc(r.error || 'Could not fetch logs')}</div></div>`;
          return;
        }
        const log = (r.log || '').trim();
        body.innerHTML = `
          <div class="card" style="padding:8px">
            <pre style="margin:0;font-size:10.5px;line-height:1.35;white-space:pre-wrap;word-break:break-word;color:var(--tg-theme-text-color);font-family:ui-monospace,Menlo,monospace;max-height:60vh;overflow:auto">${esc(log) || '(empty)'}</pre>
          </div>`;
      } catch (e) {
        document.getElementById('wl-body').innerHTML =
          `<div class="card"><div style="padding:12px;color:#ff3b30">Failed: ${esc(String(e))}</div></div>`;
      }
    };
    document.getElementById('wl-refresh').onclick = () => { haptic(); load(); };
    load();
  }


  // ── OpenClaw Skills ───────────────────────────────────────────────────────
  async function renderOpenClawSkills() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header"><span class="nav-title">⚡ Skills</span></div>
        <div id="skills-body"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    try {
      const skills = await api('/api/openclaw/skills');
      const el     = document.getElementById('skills-body');
      const state  = {};
      skills.forEach(s => state[s.name] = s.enabled);

      const enabled  = skills.filter(s => s.enabled);
      const disabled = skills.filter(s => !s.enabled);

      function skillRow(s) {
        return `<div class="settings-row skill-row">
          <div class="skill-label" data-name="${esc(s.name)}">
            <span class="skill-chevron">›</span>
            <span style="text-transform:capitalize">${esc(s.name.replace(/-/g,' '))}</span>
          </div>
          <label class="toggle" style="flex-shrink:0">
            <input type="checkbox" class="skill-tog" data-name="${esc(s.name)}" ${s.enabled ? 'checked' : ''}>
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="skill-creds-panel" data-name="${esc(s.name)}">
          <div class="skill-creds-inner"><div class="skill-creds-loading">Loading…</div></div>
        </div>`;
      }

      el.innerHTML = `
        <div class="card">
          <div class="card-section-label">Enabled (${enabled.length})</div>
          ${enabled.length ? enabled.map(skillRow).join('') : '<div class="empty-state" style="padding:12px 0;font-size:13px">None enabled</div>'}
        </div>
        ${disabled.length ? `
        <div class="card section-gap">
          <div class="card-section-label">Available (${disabled.length})</div>
          ${disabled.map(skillRow).join('')}
        </div>` : ''}
        <button class="btn btn-primary section-gap" id="skills-save-btn">Save</button>`;

      el.querySelectorAll('.skill-tog').forEach(tog => {
        tog.onchange = () => { state[tog.dataset.name] = tog.checked; };
      });

      el.querySelectorAll('.skill-label').forEach(label => {
        label.onclick = () => {
          haptic();
          const name   = label.dataset.name;
          const panel  = el.querySelector(`.skill-creds-panel[data-name="${name}"]`);
          const chev   = label.querySelector('.skill-chevron');
          const isOpen = panel.classList.contains('open');
          panel.classList.toggle('open', !isOpen);
          chev.classList.toggle('rotated', !isOpen);
          if (!isOpen && !panel.dataset.loaded) {
            panel.dataset.loaded = '1';
            _loadSkillCreds(name, panel.querySelector('.skill-creds-inner'));
          }
        };
      });

      document.getElementById('skills-save-btn').onclick = async () => {
        haptic('medium');
        const btn = document.getElementById('skills-save-btn');
        btn.textContent = 'Saving…'; btn.disabled = true;
        try {
          await api('/api/openclaw/skills', {
            method: 'POST', body: JSON.stringify({ skills: state }),
          });
          toast('Skills saved — gateway will reload');
          pop();
        } catch {
          toast('Failed to save skills');
          btn.textContent = 'Save'; btn.disabled = false;
        }
      };
    } catch {
      document.getElementById('skills-body').innerHTML =
        `<div class="card"><div class="empty-state">Failed to load skills</div></div>`;
    }
  }

  // ── Skill Credential helpers ──────────────────────────────────────────────
  async function _loadSkillCreds(skillName, container) {
    try {
      const creds = await api(`/api/openclaw/skills/${encodeURIComponent(skillName)}/credentials`);
      _renderSkillCredsInner(skillName, creds, container);
    } catch {
      container.innerHTML = '<div class="skill-creds-loading" style="color:#ff3b30">Failed to load</div>';
    }
  }

  function _renderSkillCredsInner(skillName, creds, container) {
    container.innerHTML = `
      ${creds.length ? creds.map(c => `
        <div class="cred-row">
          <span class="cred-key">${esc(c.key)}</span>
          <span class="cred-dots">••••••</span>
          <button class="btn-icon edit-cred" data-key="${esc(c.key)}" title="Edit">✎</button>
          <button class="btn-icon del-cred" data-key="${esc(c.key)}" title="Remove">×</button>
        </div>`).join('') :
        '<div class="skill-creds-loading">No credentials stored</div>'}
      <div class="cred-form-wrap"></div>
      <button class="add-cred-btn">+ Add credential</button>`;

    container.querySelectorAll('.edit-cred').forEach(btn => {
      btn.onclick = (e) => { e.stopPropagation(); haptic(); _showInlineCredForm(skillName, btn.dataset.key, container); };
    });
    container.querySelectorAll('.del-cred').forEach(btn => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete "${btn.dataset.key}"?`)) return;
        haptic();
        try {
          await api(`/api/openclaw/skills/${encodeURIComponent(skillName)}/credentials/${encodeURIComponent(btn.dataset.key)}`, { method: 'DELETE' });
          toast('Deleted');
          _loadSkillCreds(skillName, container);
        } catch { toast('Failed to delete'); }
      };
    });
    container.querySelector('.add-cred-btn').onclick = (e) => {
      e.stopPropagation(); haptic();
      _showInlineCredForm(skillName, null, container);
    };
  }

  function _showInlineCredForm(skillName, existingKey, container) {
    const wrap  = container.querySelector('.cred-form-wrap');
    const isEdit = !!existingKey;
    wrap.innerHTML = `
      <div class="cred-form">
        <div class="cred-form-label">${isEdit ? 'Edit' : 'New'} Credential</div>
        <input class="cred-input" id="ci-key" type="text" placeholder="Key  (e.g. api_key)"
               value="${isEdit ? esc(existingKey) : ''}" ${isEdit ? 'readonly' : ''}>
        <div style="display:flex;gap:6px">
          <input class="cred-input" id="ci-val" type="password" placeholder="Token / API key" style="flex:1">
          <button class="btn-icon" id="ci-eye" title="Show/hide">👁</button>
        </div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn btn-ghost" id="ci-cancel" style="flex:1;padding:6px 0;font-size:13px">Cancel</button>
          <button class="btn btn-primary" id="ci-save" style="flex:1;padding:6px 0;font-size:13px">Save</button>
        </div>
      </div>`;

    wrap.querySelector('#ci-eye').onclick   = (e) => { e.stopPropagation(); const i = wrap.querySelector('#ci-val'); i.type = i.type === 'password' ? 'text' : 'password'; };
    wrap.querySelector('#ci-cancel').onclick = (e) => { e.stopPropagation(); wrap.innerHTML = ''; };
    wrap.querySelector('#ci-save').onclick   = async (e) => {
      e.stopPropagation();
      const key   = wrap.querySelector('#ci-key').value.trim();
      const value = wrap.querySelector('#ci-val').value;
      if (!key || !value) { toast('Key and value required'); return; }
      haptic('medium');
      const btn = wrap.querySelector('#ci-save');
      btn.textContent = 'Saving…'; btn.disabled = true;
      try {
        await api(`/api/openclaw/skills/${encodeURIComponent(skillName)}/credentials`, {
          method: 'POST', body: JSON.stringify({ key, value }),
        });
        toast('Saved to Credential Manager');
        wrap.innerHTML = '';
        _loadSkillCreds(skillName, container);
      } catch {
        toast('Failed to save');
        btn.textContent = 'Save'; btn.disabled = false;
      }
    };

    if (!isEdit) wrap.querySelector('#ci-key').focus();
    else         wrap.querySelector('#ci-val').focus();
  }

  // ── Keys card (Secrets rotation) ──────────────────────────────────────────
  async function _loadKeysList() {
    const el = document.getElementById('keys-list');
    if (!el) return;
    try {
      const list = await api('/api/openclaw/secrets');
      const groups = {};
      list.forEach(s => { (groups[s.category] = groups[s.category] || []).push(s); });
      // Sort items within each category alphabetically by label
      Object.values(groups).forEach(items => items.sort((a, b) => a.label.localeCompare(b.label)));
      // Sort categories alphabetically (e.g. Bots, Cloud APIs, Interactive, Internal)
      const sortedCategories = Object.keys(groups).sort((a, b) => a.localeCompare(b));

      const statusLabel = s => {
        if (s.status === 'external')   return 'external';
        if (s.status === 'oauth-only') return 'OAuth (token on disk)';
        if (s.status === 'missing')    return '<missing>';
        if (s.storage === 'wcm')       return 'secured · WCM';
        if (s.storage === 'openclaw')  return 'secured · openclaw';
        if (s.storage === 'envfile')   return 'secured · env file';
        return `secured · ${esc(s.storage)}`;
      };

      el.innerHTML = sortedCategories.map(cat => `
        <div class="keys-group">
          <div class="keys-group-label keys-group-toggle" data-cat="${esc(cat)}">
            <span>${esc(cat)}</span>
            <span class="keys-group-arrow">⌄</span>
          </div>
          <div class="keys-group-items">
          ${groups[cat].map(s => `
            <div class="key-row" data-name="${esc(s.name)}">
              <div class="key-row-head">
                <div class="key-row-label">${esc(s.label)}</div>
                <span class="key-status key-status-${s.status}">${statusLabel(s)}</span>
                <span class="key-row-arrow">›</span>
              </div>
              <div class="key-row-body" hidden>
                <div class="key-section">
                  <div class="key-section-label">How to retrieve</div>
                  <ol class="key-steps">${s.retrieval.map(step => `<li>${esc(step)}</li>`).join('')}</ol>
                </div>
                <div class="key-section">
                  <div class="key-section-label">⚠ Revoke the old key</div>
                  <div class="key-revoke">${esc(s.revoke)}</div>
                </div>
                <div class="key-form-wrap"></div>
                <div class="key-actions">
                  ${s.instructions_only ? '' : (
                    s.external_oauth ? `<button class="btn btn-primary key-action-oauth" data-url="${esc(s.external_oauth)}" style="flex:1;padding:6px 0;font-size:13px">Open re-auth URL</button>` :
                    s.regen_only ? '<button class="btn btn-primary key-action-regen" style="flex:1;padding:6px 0;font-size:13px">Reset & new QR</button>' :
                    `${s.autogen ? '<button class="btn btn-ghost key-action-autogen" style="flex:1;padding:6px 0;font-size:13px">Auto-generate</button>' : ''}
                     ${s.has_test && s.status === 'set' ? '<button class="btn btn-ghost key-action-test" style="flex:1;padding:6px 0;font-size:13px">Test current</button>' : ''}
                     <button class="btn btn-primary key-action-rotate" style="flex:1;padding:6px 0;font-size:13px">${s.status === 'set' ? 'Rotate' : 'Add'}</button>`
                  )}
                </div>
              </div>
            </div>
          `).join('')}
          </div>
        </div>`).join('');

      // Wire up expand/collapse + actions
      // Category collapsibles — click header to toggle item visibility
      el.querySelectorAll('.keys-group-toggle').forEach(header => {
        header.onclick = () => {
          haptic();
          const items = header.parentElement.querySelector('.keys-group-items');
          const arrow = header.querySelector('.keys-group-arrow');
          const open = !items.hidden;
          items.hidden = open;
          arrow.textContent = open ? '›' : '⌄';
        };
      });

      el.querySelectorAll('.key-row').forEach(row => {
        const head = row.querySelector('.key-row-head');
        const body = row.querySelector('.key-row-body');
        const arrow = row.querySelector('.key-row-arrow');
        head.onclick = () => {
          haptic();
          const open = !body.hidden;
          // collapse all peers
          el.querySelectorAll('.key-row .key-row-body').forEach(b => b.hidden = true);
          el.querySelectorAll('.key-row .key-row-arrow').forEach(a => a.textContent = '›');
          if (!open) {
            body.hidden = false;
            arrow.textContent = '⌄';
          }
        };

        const rotateBtn = row.querySelector('.key-action-rotate');
        if (rotateBtn) {
          rotateBtn.onclick = (e) => {
            e.stopPropagation(); haptic();
            _showKeyRotateForm(row.dataset.name, row.querySelector('.key-form-wrap'));
          };
        }
        const autogenBtn = row.querySelector('.key-action-autogen');
        if (autogenBtn) {
          autogenBtn.onclick = async (e) => {
            e.stopPropagation();
            if (!confirm(`Auto-generate a new value for "${row.dataset.name}"? This replaces the current one.`)) return;
            haptic('medium');
            autogenBtn.textContent = 'Working…'; autogenBtn.disabled = true;
            try {
              const r = await api(`/api/openclaw/secrets/${encodeURIComponent(row.dataset.name)}/rotate`, {
                method: 'POST', body: JSON.stringify({})
              });
              toast(r.ok ? 'Auto-generated and saved' : `Failed: ${r.detail || 'unknown'}`);
              _loadKeysList();
            } catch { toast('Rotation failed'); autogenBtn.textContent = 'Auto-generate'; autogenBtn.disabled = false; }
          };
        }
        const testBtn = row.querySelector('.key-action-test');
        if (testBtn) {
          testBtn.onclick = async (e) => {
            e.stopPropagation(); haptic();
            testBtn.textContent = 'Testing…'; testBtn.disabled = true;
            try {
              const r = await api(`/api/openclaw/secrets/${encodeURIComponent(row.dataset.name)}/test`, { method: 'POST' });
              toast(r.ok ? `✓ ${r.detail || 'OK'}` : `✗ ${r.detail || 'failed'}`);
            } catch { toast('Test request failed'); }
            testBtn.textContent = 'Test current'; testBtn.disabled = false;
          };
        }
        const oauthBtn = row.querySelector('.key-action-oauth');
        if (oauthBtn) {
          oauthBtn.onclick = (e) => {
            e.stopPropagation(); haptic();
            const url = oauthBtn.dataset.url;
            if (tg && tg.openLink) tg.openLink(url);
            else window.open(url, '_blank');
            toast('Re-auth URL opened — complete the flow in your browser');
          };
        }
        const regenBtn = row.querySelector('.key-action-regen');
        if (regenBtn) {
          regenBtn.onclick = async (e) => {
            e.stopPropagation();
            if (!confirm('Reset TOTP secret? Your existing authenticator entry will stop working — you must scan the new QR.')) return;
            haptic('medium');
            regenBtn.textContent = 'Resetting…'; regenBtn.disabled = true;
            try {
              const r = await api(`/api/openclaw/secrets/${encodeURIComponent(row.dataset.name)}/rotate`, { method: 'POST', body: JSON.stringify({ regen: true }) });
              if (r.ok) {
                toast('TOTP reset — open totp_setup.html and scan the new QR');
              } else {
                toast(`Reset failed: ${r.detail || 'unknown'}`);
              }
            } catch { toast('Reset request failed'); }
            regenBtn.textContent = 'Reset & new QR'; regenBtn.disabled = false;
          };
        }
      });
    } catch (e) {
      el.innerHTML = '<div class="skill-creds-loading" style="color:#ff3b30">Failed to load keys</div>';
    }
  }

  function _showKeyRotateForm(name, wrap) {
    wrap.innerHTML = `
      <div class="cred-form" style="margin-top:10px">
        <div class="cred-form-label">Paste new value</div>
        <div style="display:flex;gap:6px">
          <input class="cred-input" id="kr-val-${esc(name)}" type="password" placeholder="New token / API key" style="flex:1" autocomplete="off">
          <button class="btn-icon" id="kr-eye-${esc(name)}" title="Show/hide">👁</button>
        </div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn btn-ghost" id="kr-cancel-${esc(name)}" style="flex:1;padding:6px 0;font-size:13px">Cancel</button>
          <button class="btn btn-primary" id="kr-save-${esc(name)}" style="flex:1;padding:6px 0;font-size:13px">Save & rotate</button>
        </div>
      </div>`;
    const valI = wrap.querySelector(`#kr-val-${CSS.escape(name)}`);
    const eye  = wrap.querySelector(`#kr-eye-${CSS.escape(name)}`);
    eye.onclick    = (e) => { e.stopPropagation(); valI.type = valI.type === 'password' ? 'text' : 'password'; };
    wrap.querySelector(`#kr-cancel-${CSS.escape(name)}`).onclick = (e) => { e.stopPropagation(); wrap.innerHTML = ''; };
    wrap.querySelector(`#kr-save-${CSS.escape(name)}`).onclick   = async (e) => {
      e.stopPropagation();
      const value = valI.value;
      if (!value || value.length < 8) { toast('Value too short'); return; }
      haptic('medium');
      const btn = wrap.querySelector(`#kr-save-${CSS.escape(name)}`);
      btn.textContent = 'Rotating…'; btn.disabled = true;
      try {
        const r = await api(`/api/openclaw/secrets/${encodeURIComponent(name)}/rotate`, {
          method: 'POST', body: JSON.stringify({ value }),
        });
        if (r.ok) {
          let msg = 'Rotated';
          if (r.smoke_test) msg += r.smoke_test.ok ? ` · ✓ ${r.smoke_test.detail}` : ` · ⚠ smoke: ${r.smoke_test.detail}`;
          if (r.manual_restart_needed && r.manual_restart_needed.length) msg += ` · ⚠ restart manually: ${r.manual_restart_needed.join(', ')}`;
          toast(msg);
          wrap.innerHTML = '';
          _loadKeysList();
        } else {
          toast(`Failed: ${r.detail || 'unknown'}`);
          btn.textContent = 'Save & rotate'; btn.disabled = false;
        }
      } catch {
        toast('Rotation failed');
        btn.textContent = 'Save & rotate'; btn.disabled = false;
      }
    };
    valI.focus();
  }

  // ── OpenClaw Doctor ───────────────────────────────────────────────────────
  async function renderOpenClawDoctor() {
    app.innerHTML = `
      <div class="page">
        <div class="nav-header">
          <span class="nav-title">🩺 OpenClaw Doctor</span>
          <button class="btn btn-ghost" id="doc-refresh" style="width:auto;padding:4px 10px;font-size:18px">↻</button>
        </div>
        <div id="doctor-body"><div class="card"><div class="spinner" style="margin:24px auto"></div></div></div>
      </div>`;
    document.getElementById('doc-refresh').onclick = () => { haptic(); _loadDoctor(); };
    _loadDoctor();
  }

  async function _loadDoctor() {
    const el = document.getElementById('doctor-body');
    if (!el) return;
    el.innerHTML = `<div class="card"><div class="spinner" style="margin:24px auto"></div></div>`;
    try {
      const data   = await api('/api/openclaw/doctor');
      const checks = data.checks || [];
      const logs   = data.logs   || [];
      const pass   = checks.filter(c => c.ok).length;
      const summaryColor = pass === checks.length ? '#4cd964' : pass >= checks.length - 1 ? '#ff9500' : '#ff3b30';
      el.innerHTML = `
        <div class="card">
          <div class="card-section-label">Checks
            <span style="font-weight:500;color:${summaryColor};text-transform:none;letter-spacing:0">&nbsp;${pass}/${checks.length} passed</span>
          </div>
          ${checks.map(c => `
            <div class="wd-item">
              <div class="status-dot ${c.ok ? 'active' : 'inactive'}" style="width:7px;height:7px;flex-shrink:0"></div>
              <div class="wd-item-name">${esc(c.name)}</div>
              <div class="wd-item-detail">${esc(c.detail)}</div>
            </div>`).join('')}
        </div>
        ${logs.length ? `
        <div class="card section-gap">
          <div class="card-section-label">Recent Logs</div>
          <pre class="doctor-log">${esc(logs.join('\n'))}</pre>
        </div>` : ''}`;
    } catch {
      el.innerHTML = `<div class="card"><div class="empty-state">Doctor check failed</div></div>`;
    }
  }

  // ── Bootstrap ─────────────────────────────────────────────────────────────
  // Adopt any server-issued APK session token before checking for an existing
  // session — bypasses Telegram-login + TOTP when opened via the Suite APK.
  _adoptApkSession();

  if (_getSession()) {
    navStack.push({ screen: 'home', data: {} });
    render('home');
    // Phase 1.4 — deep-link to a specific panel via ?panel=<name>
    const dl = _maybeDeepLinkPanel();
    if (dl) {
      // Defer one tick so home renders first, then push the target panel
      setTimeout(() => push(dl), 50);
    }
  } else {
    renderLogin();
  }
})();
