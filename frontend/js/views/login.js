/**
 * Auth gate — owner setup, login, and the TOTP second-factor step.
 *
 * Rendered full-screen before any nav/view mounts. Resolves only once the
 * browser holds a valid session (or the install has login disabled / edge
 * identity). On success it reloads so boot re-runs cleanly with the cookie set.
 */

import { get, post } from '../api.js';
import { mountChecklist } from '../components/password-policy.js';

const STYLE = `
.agd-auth-overlay{position:fixed;inset:0;z-index:10000;display:flex;flex-direction:column;
  align-items:center;justify-content:center;background:var(--bg-primary,#0f1115);padding:24px}
.agd-auth-brand{font-size:34px;font-weight:700;letter-spacing:-0.6px;margin:0 0 24px;
  text-align:center;color:var(--text-primary,#e8eaed);line-height:1}
.agd-auth-brand .accent{color:var(--accent,#ff6d5a)}
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
.agd-auth-field-note{margin-top:5px;font-size:11px;color:var(--text-secondary,#9aa0aa);
  line-height:1.4}
.agd-auth-row{display:flex;justify-content:flex-end;margin-top:12px}
.agd-auth-link{font-size:12px;color:var(--accent,#ff6d5a);text-decoration:none;cursor:pointer}
.agd-auth-link:hover{text-decoration:underline}
.agd-auth-ok{margin-top:14px;font-size:13px;color:var(--success,#34d399);line-height:1.5}
.agd-auth-back{margin-top:16px;font-size:12px;color:var(--text-secondary,#9aa0aa);
  background:none;border:none;cursor:pointer;padding:0}
.agd-auth-back:hover{color:var(--text-primary,#e8eaed)}
`;

// Brand wordmark shown centered above every auth card. Mirrors the sidebar logo
// (`Agenius` + accent `Desk`) so it stays consistent across themes.
const BRAND = '<div class="agd-auth-brand">Agenius<span class="accent">Desk</span></div>';

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

// Creating the owner account is a fresh install: clear any onboarding state left
// in this browser (coachmark "seen" flags, get-started/connect/error dismissals)
// so the new operator always gets the full walkthrough, even if this origin's
// localStorage carries flags from a previous install.
function resetOnboardingState() {
  try {
    const kill = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && (k.startsWith('agd_tour_seen:') || k.startsWith('agd_seen:'))) kill.push(k);
    }
    kill.push(
      'agd_tips_enabled', 'agd_getstarted_dismissed', 'agd_connect_n8n_dismissed',
      'agd_error_handler_prompted', 'agd_welcome_n8n_dismissed', 'agd_welcome_empty_dismissed',
    );
    kill.forEach(k => localStorage.removeItem(k));
  } catch { /* ignore */ }
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function renderSetup(policy) {
  const overlay = el(`
    <div class="agd-auth-overlay">
      ${BRAND}
      <form class="agd-auth-card" autocomplete="off">
        <h1>Create your owner account</h1>
        <p class="agd-auth-sub">This account secures your AgeniusDesk install.</p>
        <label>Email</label>
        <input id="a-email" type="email" autocomplete="email" autofocus
          placeholder="you@example.com">
        <div class="agd-auth-field-note">You sign in with this email. It is also used
          to reset your password.</div>
        <label>Display name (optional)</label>
        <input id="a-name" autocomplete="off">
        <label>Password</label>
        <input id="a-pass" type="password" autocomplete="new-password">
        <div id="a-pwck"></div>
        <label>Confirm password</label>
        <input id="a-pass2" type="password" autocomplete="new-password">
        <button class="agd-auth-btn" type="submit">Create account</button>
        <div class="agd-auth-err" id="a-err"></div>
      </form>
    </div>`);
  mount(overlay);
  const err = overlay.querySelector('#a-err');
  const checklist = mountChecklist(overlay.querySelector('#a-pwck'), overlay.querySelector('#a-pass'), policy);
  overlay.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    const email = overlay.querySelector('#a-email').value.trim();
    const display_name = overlay.querySelector('#a-name').value.trim();
    const password = overlay.querySelector('#a-pass').value;
    const password2 = overlay.querySelector('#a-pass2').value;
    if (!EMAIL_RE.test(email)) { err.textContent = 'Enter a valid email address'; return; }
    if (!checklist.isValid()) { err.textContent = 'Password does not meet the requirements below'; return; }
    if (password !== password2) { err.textContent = 'Passwords do not match'; return; }
    const btn = overlay.querySelector('button');
    btn.disabled = true;
    try {
      await post('/api/auth/setup', { email, password, display_name });
      resetOnboardingState();  // fresh install → fresh walkthrough
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
      ${BRAND}
      <form class="agd-auth-card" autocomplete="on">
        <h1>Sign in</h1>
        <p class="agd-auth-sub">Welcome back to AgeniusDesk.</p>
        <label>Email</label>
        <input id="a-user" type="email" autocomplete="username" autofocus
          placeholder="you@example.com">
        <label>Password</label>
        <input id="a-pass" type="password" autocomplete="current-password">
        <button class="agd-auth-btn" type="submit">Sign in</button>
        <div class="agd-auth-row">
          <a href="#" class="agd-auth-link" id="a-forgot">Forgot password?</a>
        </div>
        <div class="agd-auth-err" id="a-err"></div>
      </form>
    </div>`);
  mount(overlay);
  const err = overlay.querySelector('#a-err');
  overlay.querySelector('#a-forgot').addEventListener('click', (e) => {
    e.preventDefault();
    renderForgot();
  });
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
      ${BRAND}
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

function renderForgot() {
  const overlay = el(`
    <div class="agd-auth-overlay">
      ${BRAND}
      <form class="agd-auth-card" autocomplete="on">
        <h1>Reset your password</h1>
        <p class="agd-auth-sub">Enter your account email and we'll send a reset link.</p>
        <label>Email</label>
        <input id="a-email" type="email" autocomplete="email" autofocus
          placeholder="you@example.com">
        <button class="agd-auth-btn" type="submit">Send reset link</button>
        <button class="agd-auth-back" type="button" id="a-back">&larr; Back to sign in</button>
        <div class="agd-auth-err" id="a-err"></div>
      </form>
    </div>`);
  mount(overlay);
  const err = overlay.querySelector('#a-err');
  overlay.querySelector('#a-back').addEventListener('click', () => renderLogin());
  overlay.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    const email = overlay.querySelector('#a-email').value.trim();
    if (!EMAIL_RE.test(email)) { err.textContent = 'Enter a valid email address'; return; }
    const btn = overlay.querySelector('.agd-auth-btn');
    btn.disabled = true;
    try {
      await post('/api/auth/forgot', { email });
    } catch { /* deliberately ignore — never reveal whether the email exists */ }
    // Always show the same confirmation regardless of whether the email matched.
    overlay.querySelector('.agd-auth-card').innerHTML = `
      <h1>Check your email</h1>
      <p class="agd-auth-ok">If an account exists for <strong>${escapeHtml(email)}</strong>,
        a password reset link is on its way. It expires shortly.</p>
      <p class="agd-auth-hint">Didn't get it? Check spam, or confirm your install has
        SMTP configured (otherwise the link is written to the server log).</p>
      <button class="agd-auth-back" type="button" id="a-back2">&larr; Back to sign in</button>`;
    overlay.querySelector('#a-back2').addEventListener('click', () => renderLogin());
  });
}

function renderReset(token, policy) {
  const overlay = el(`
    <div class="agd-auth-overlay">
      ${BRAND}
      <form class="agd-auth-card" autocomplete="off">
        <h1>Choose a new password</h1>
        <p class="agd-auth-sub">Set a new password for your account.</p>
        <label>New password</label>
        <input id="a-pass" type="password" autocomplete="new-password" autofocus>
        <div id="a-pwck"></div>
        <label>Confirm new password</label>
        <input id="a-pass2" type="password" autocomplete="new-password">
        <button class="agd-auth-btn" type="submit">Reset password</button>
        <div class="agd-auth-err" id="a-err"></div>
      </form>
    </div>`);
  mount(overlay);
  const err = overlay.querySelector('#a-err');
  const checklist = mountChecklist(overlay.querySelector('#a-pwck'), overlay.querySelector('#a-pass'), policy);
  overlay.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    const pw = overlay.querySelector('#a-pass').value;
    const pw2 = overlay.querySelector('#a-pass2').value;
    if (!checklist.isValid()) { err.textContent = 'Password does not meet the requirements below'; return; }
    if (pw !== pw2) { err.textContent = 'Passwords do not match'; return; }
    const btn = overlay.querySelector('.agd-auth-btn');
    btn.disabled = true;
    try {
      await post('/api/auth/reset', { token, new_password: pw });
      overlay.querySelector('.agd-auth-card').innerHTML = `
        <h1>Password updated</h1>
        <p class="agd-auth-ok">Your password has been changed and other sessions were
          signed out. You can sign in now.</p>
        <button class="agd-auth-btn" type="button" id="a-signin">Go to sign in</button>`;
      overlay.querySelector('#a-signin').addEventListener('click', goToCleanLogin);
    } catch (ex) {
      err.textContent = ex.message || 'Could not reset password';
      btn.disabled = false;
    }
  });
}

// Strip the ?reset=... token from the URL, then reload into the normal gate.
function goToCleanLogin() {
  const url = location.origin + location.pathname;
  location.replace(url);
}

function escapeHtml(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : s;
  return d.innerHTML;
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
  // A password-reset deep link always wins, even for an authenticated session.
  const resetToken = new URLSearchParams(location.search).get('reset');
  if (resetToken) {
    document.cookie = 'agd_csrf=; Max-Age=0; Path=/; SameSite=Strict';
    injectStyle();
    renderReset(resetToken, status.password_policy);
    return new Promise(() => {});
  }
  if (status.login_disabled || status.edge_identity || status.authenticated) return true;
  // Clear any stale readable CSRF cookie so background 401s don't trigger the
  // session-expiry reload while we are (correctly) sitting on the auth gate.
  document.cookie = 'agd_csrf=; Max-Age=0; Path=/; SameSite=Strict';
  injectStyle();
  if (status.accounts_exist) renderLogin();
  else renderSetup(status.password_policy);
  return new Promise(() => {});  // never resolves; success reloads
}
