/**
 * Auth gate — owner setup, login, and the TOTP second-factor step.
 *
 * Rendered full-screen before any nav/view mounts. Resolves only once the
 * browser holds a valid session (or the install has login disabled / edge
 * identity). On success it reloads so boot re-runs cleanly with the cookie set.
 */

import { get, post } from '../api.js';

const STYLE = `
.agd-auth-overlay{position:fixed;inset:0;z-index:10000;display:flex;align-items:center;
  justify-content:center;background:var(--bg-primary,#0f1115);padding:24px}
.agd-auth-card{width:100%;max-width:380px;background:var(--bg-elevated,#1a1d24);
  border:1px solid var(--border,#2a2f3a);border-radius:14px;padding:32px 28px;
  box-shadow:0 12px 48px rgba(0,0,0,.45)}
.agd-auth-card h1{font-size:20px;margin:0 0 4px;color:var(--text-primary,#e8eaed)}
.agd-auth-sub{font-size:13px;color:var(--text-secondary,#9aa0aa);margin:0 0 22px}
.agd-auth-card label{display:block;font-size:12px;color:var(--text-secondary,#9aa0aa);
  margin:14px 0 5px}
.agd-auth-card input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;
  border:1px solid var(--border,#2a2f3a);background:var(--bg-primary,#0f1115);
  color:var(--text-primary,#e8eaed);font-size:14px}
.agd-auth-card input:focus{outline:none;border-color:var(--accent,#ff6d5a)}
.agd-auth-btn{width:100%;margin-top:22px;padding:11px;border:none;border-radius:8px;
  background:var(--accent,#ff6d5a);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
.agd-auth-btn:disabled{opacity:.6;cursor:default}
.agd-auth-err{margin-top:14px;font-size:13px;color:#f87171;min-height:16px}
.agd-auth-hint{margin-top:18px;font-size:12px;color:var(--text-secondary,#9aa0aa);
  line-height:1.5}
`;

function injectStyle() {
  if (document.getElementById('agd-auth-style')) return;
  const s = document.createElement('style');
  s.id = 'agd-auth-style';
  s.textContent = STYLE;
  document.head.appendChild(s);
}

function el(html) {
  const d = document.createElement('div');
  d.innerHTML = html.trim();
  return d.firstElementChild;
}

function mount(node) {
  document.querySelectorAll('.agd-auth-overlay').forEach(n => n.remove());
  document.body.appendChild(node);
}

function renderSetup() {
  const overlay = el(`
    <div class="agd-auth-overlay">
      <form class="agd-auth-card" autocomplete="off">
        <h1>Create your owner account</h1>
        <p class="agd-auth-sub">This account secures your AgeniusDesk install.</p>
        <label>Username</label>
        <input id="a-user" autocomplete="username" autofocus>
        <label>Display name (optional)</label>
        <input id="a-name" autocomplete="off">
        <label>Password</label>
        <input id="a-pass" type="password" autocomplete="new-password">
        <label>Confirm password</label>
        <input id="a-pass2" type="password" autocomplete="new-password">
        <button class="agd-auth-btn" type="submit">Create account</button>
        <div class="agd-auth-err" id="a-err"></div>
      </form>
    </div>`);
  mount(overlay);
  const err = overlay.querySelector('#a-err');
  overlay.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    const username = overlay.querySelector('#a-user').value.trim();
    const display_name = overlay.querySelector('#a-name').value.trim();
    const password = overlay.querySelector('#a-pass').value;
    const password2 = overlay.querySelector('#a-pass2').value;
    if (!username) { err.textContent = 'Username is required'; return; }
    if (password !== password2) { err.textContent = 'Passwords do not match'; return; }
    const btn = overlay.querySelector('button');
    btn.disabled = true;
    try {
      await post('/api/auth/setup', { username, password, display_name });
      location.reload();
    } catch (ex) {
      err.textContent = ex.message || 'Could not create account';
      btn.disabled = false;
    }
  });
}

function renderLogin() {
  const overlay = el(`
    <div class="agd-auth-overlay">
      <form class="agd-auth-card" autocomplete="on">
        <h1>Sign in</h1>
        <p class="agd-auth-sub">Welcome back to AgeniusDesk.</p>
        <label>Username</label>
        <input id="a-user" autocomplete="username" autofocus>
        <label>Password</label>
        <input id="a-pass" type="password" autocomplete="current-password">
        <button class="agd-auth-btn" type="submit">Sign in</button>
        <div class="agd-auth-err" id="a-err"></div>
      </form>
    </div>`);
  mount(overlay);
  const err = overlay.querySelector('#a-err');
  overlay.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    const username = overlay.querySelector('#a-user').value.trim();
    const password = overlay.querySelector('#a-pass').value;
    const btn = overlay.querySelector('button');
    btn.disabled = true;
    try {
      const r = await post('/api/auth/login', { username, password });
      if (r.totp_required) { renderTotp(r.pending_token); return; }
      location.reload();
    } catch (ex) {
      err.textContent = ex.message || 'Sign in failed';
      btn.disabled = false;
    }
  });
}

function renderTotp(pendingToken) {
  const overlay = el(`
    <div class="agd-auth-overlay">
      <form class="agd-auth-card" autocomplete="off">
        <h1>Two-factor code</h1>
        <p class="agd-auth-sub">Enter the 6-digit code from your authenticator app.</p>
        <label>Code</label>
        <input id="a-code" inputmode="numeric" autocomplete="one-time-code" autofocus
          placeholder="123456">
        <button class="agd-auth-btn" type="submit">Verify</button>
        <div class="agd-auth-err" id="a-err"></div>
        <div class="agd-auth-hint">Lost your device? Enter one of your recovery codes
          instead of the 6-digit code.</div>
      </form>
    </div>`);
  mount(overlay);
  const err = overlay.querySelector('#a-err');
  overlay.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    const code = overlay.querySelector('#a-code').value.trim();
    const btn = overlay.querySelector('button');
    btn.disabled = true;
    try {
      await post('/api/auth/login/totp', { pending_token: pendingToken, code });
      location.reload();
    } catch (ex) {
      err.textContent = ex.message || 'Invalid code';
      btn.disabled = false;
    }
  });
}

/**
 * Resolve true when the app may proceed to boot; otherwise render the gate and
 * never resolve (the successful auth path reloads the page).
 */
export async function requireAuth() {
  let status;
  try {
    status = await get('/api/auth/status');
  } catch {
    // Auth endpoint unreachable — fail open so a broken auth module never
    // bricks the whole dashboard. The HTTP gate still protects the API.
    return true;
  }
  if (status.login_disabled || status.edge_identity || status.authenticated) return true;
  // Clear any stale readable CSRF cookie so background 401s don't trigger the
  // session-expiry reload while we are (correctly) sitting on the auth gate.
  document.cookie = 'agd_csrf=; Max-Age=0; Path=/; SameSite=Strict';
  injectStyle();
  if (status.accounts_exist) renderLogin();
  else renderSetup();
  return new Promise(() => {});  // never resolves; success reloads
}
