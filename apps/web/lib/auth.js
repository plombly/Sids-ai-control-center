// Sign-in (apps/api/auth.py): the first-run administrator account (with the
// one-time setup code from the server), the sign-in screen, the user menu,
// and Settings → Access (password, sessions, audit log).
import { requestJSON } from './api.js';
import { esc, escValue } from './format.js';

export function setupMarkup(message = '') {
  return `<form id="auth-setup-form" class="auth-card" autocomplete="off"><img class="auth-logo" src="brand/logo.svg" alt="" width="72" height="72"><h1>Welcome to <span class="wordmark">L<span class="wordmark-ai">AI</span>ka</span></h1><p class="subtle">Create the administrator account. You need the setup code the installer printed; run <code>sudo laika setup-code</code> on the server for a new one.</p><label class="field">Setup code<input name="code" required autocomplete="one-time-code" placeholder="ABCD-2345" autocapitalize="characters"></label><label class="field">Username<input name="username" required autocomplete="username" pattern="[A-Za-z0-9._-]{2,40}"></label><label class="field">Password<input name="password" type="password" required minlength="10" autocomplete="new-password"></label><label class="field">Repeat the password<input name="repeat" type="password" required minlength="10" autocomplete="new-password"></label><button type="submit" class="primary">Create account</button><span class="form-status" role="status">${message ? esc(message) : ''}</span><p class="subtle auth-note">LAIka is meant for your own network or VPN only. Never put it on the public internet.</p></form>`;
}

export function loginMarkup(message = '') {
  return `<form id="auth-login-form" class="auth-card"><img class="auth-logo" src="brand/logo.svg" alt="" width="72" height="72"><h1>Sign in to <span class="wordmark">L<span class="wordmark-ai">AI</span>ka</span></h1><label class="field">Username<input name="username" required autocomplete="username"></label><label class="field">Password<input name="password" type="password" required autocomplete="current-password"></label><button type="submit" class="primary">Sign in</button><span class="form-status" role="status">${message ? esc(message) : ''}</span><p class="subtle auth-note">Forgot the password? Run <code>sudo laika reset-password</code> on the server.</p></form>`;
}

// Which screen the sign-in state calls for: 'setup', 'login' or '' (none).
export function screenFor(state) {
  if (state?.setup_required) return 'setup';
  if (state?.admin_exists && !state.signed_in) return 'login';
  return '';
}

export const userMenuMarkup = user => (user ? `<span class="subtle">${esc(user)}</span><button type="button" class="detail-button" data-sign-out>Sign out</button>` : '');

function when(seconds) {
  if (!seconds) return '—';
  return new Date(seconds * 1000).toLocaleString();
}

export function accessMarkup(sessions = [], entries = []) {
  const rows = sessions
    .map(item => `<li class="device-row"><span><b>${esc(item.user)}</b>${item.current ? ' <span class="pill ok">this browser</span>' : ''}<span class="subtle"> · ${esc(item.ip || '')} · last used ${esc(when(item.last_seen))}</span><br><span class="subtle">${esc((item.agent || '').slice(0, 90))}</span></span>${item.current ? '' : `<button type="button" class="danger-button" data-end-session="${escValue(item.id)}">Sign out</button>`}</li>`)
    .join('');
  const log = entries
    .map(entry => `<tr><td class="subtle">${esc(when(entry.at))}</td><td>${esc(entry.actor)}</td><td><code>${esc(entry.method)} ${esc(entry.path)}</code></td><td>${esc(entry.status)}</td><td class="subtle">${esc(entry.ip || '')}</td></tr>`)
    .join('');
  return `<form id="auth-password-form" class="settings-card"><h3>Your password</h3><label class="field">Current password<input name="current" type="password" required autocomplete="current-password"></label><label class="field">New password (10 characters or more)<input name="new" type="password" required minlength="10" autocomplete="new-password"></label><div class="settings-actions"><button type="submit">Change password</button><span class="form-status" role="status"></span></div><p class="subtle">Changing it signs out every other browser.</p></form><div class="settings-card"><h3>Signed-in browsers</h3><ul class="device-list">${rows || '<li class="subtle">None</li>'}</ul></div><div class="settings-card"><h3>Audit log</h3><p class="subtle">Every change made through LAIka: who, what and when (newest first).</p><div class="table-wrap"><table class="job-table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Result</th><th>From</th></tr></thead><tbody>${log || '<tr><td colspan="5" class="subtle">Nothing yet</td></tr>'}</tbody></table></div></div>`;
}

export async function accessData() {
  const [sessions, audit] = await Promise.all([requestJSON('/api/auth/sessions'), requestJSON('/api/audit?limit=200')]);
  return accessMarkup(sessions.sessions, audit.entries);
}

if (typeof document !== 'undefined') {
  const screen = () => document.getElementById('auth-screen');
  // The dashboard keeps polling while signed out, and every 401 asks for a
  // check: draw a screen only when it changes, or typing would be wiped.
  let shown = null;
  const show = (kind, html) => {
    const node = screen();
    if (!node || kind === shown) return;
    shown = kind;
    node.innerHTML = html;
    node.hidden = !html;
    document.body.classList.toggle('auth-locked', Boolean(html));
    node.querySelector('input')?.focus();
  };
  const say = (form, message) => {
    const node = form.querySelector('.form-status');
    if (node) node.textContent = message;
  };
  async function check() {
    try {
      const state = await requestJSON('/api/auth/state');
      const menu = document.getElementById('user-menu');
      if (menu) menu.innerHTML = userMenuMarkup(state.user);
      const kind = screenFor(state);
      if (kind === 'setup') show(kind, setupMarkup());
      else if (kind === 'login') show(kind, loginMarkup());
      else {
        show('', '');
        if (state.signed_in && !location.hash.startsWith('#/setup')) {
          const setup = await requestJSON('/api/setup/state').catch(() => ({ done: true }));
          if (setup.done === false) location.hash = '#/setup';
        }
      }
    } catch {}
  }
  window.addEventListener('laika:auth-required', check);
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!['auth-setup-form', 'auth-login-form', 'auth-password-form'].includes(form?.id)) return;
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    try {
      if (form.id === 'auth-setup-form') {
        if (values.password !== values.repeat) return say(form, 'The passwords do not match');
        await requestJSON('/api/setup/admin', { method: 'POST', body: JSON.stringify({ code: values.code, username: values.username, password: values.password }) });
        location.hash = '#/setup';
        location.reload();
      } else if (form.id === 'auth-login-form') {
        await requestJSON('/api/auth/login', { method: 'POST', body: JSON.stringify({ username: values.username, password: values.password }) });
        location.reload();
      } else {
        await requestJSON('/api/auth/password', { method: 'POST', body: JSON.stringify({ current: values.current, new: values.new }) });
        form.reset();
        say(form, 'Password changed');
      }
    } catch (error) {
      say(form, error.message);
    }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.signOut !== undefined) {
      await requestJSON('/api/auth/logout', { method: 'POST' }).catch(() => {});
      location.reload();
    } else if (button.dataset.endSession) {
      button.disabled = true;
      await requestJSON(`/api/auth/sessions/${encodeURIComponent(button.dataset.endSession)}`, { method: 'DELETE' }).catch(() => {});
      window.dispatchEvent(new CustomEvent('laika:settings-redraw'));
    }
  });
  check();
}
