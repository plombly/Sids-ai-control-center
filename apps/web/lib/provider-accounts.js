// Settings → AI, More accounts: several Claude and Codex accounts (each a
// subscription sign-in or an API key). LAIka gives each run to the account
// with the most room left and pauses one account at a time
// (services/provider_accounts.py; API apps/api/provider_routes.py).
import { requestJSON, newRequestId } from './api.js';
import { esc, escValue } from './format.js';
import { registerClick } from './registry.js';

const NAMES = { claude: 'Claude', codex: 'Codex' };

const clock = epoch => {
  const date = new Date(Number(epoch) * 1000);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
};

const usage = reading => {
  const windows = Object.values(reading?.windows || {}).filter(w => typeof w?.percent === 'number');
  return windows.length ? `${Math.round(Math.max(...windows.map(w => w.percent)))}% of plan used` : '';
};

export function stateMarkup(account) {
  if (!account.enabled) return '<span class="pill">Turned off</span>';
  if (account.hold?.held) return `<span class="pill warn">Paused until ${esc(clock(account.hold.until))}</span>`;
  if (!account.signed_in) return `<span class="pill warn">${account.kind === 'key' && !account.key_saved ? 'No key yet' : 'Not signed in'}</span>`;
  return '<span class="pill ok">Taking work</span>';
}

export function progressMarkup(login, provider, account) {
  if (!login || (login.account || 'default') !== account) return '';
  if (login.state === 'waiting' && login.url) {
    const step2 = login.code
      ? `<p>2. Enter this code there: <b class="login-code">${esc(login.code)}</b></p>`
      : `<p>2. Paste the code it shows you:</p><div class="form-row"><input name="account_code" placeholder="Code from Claude" autocomplete="off"><button type="button" data-account-code="${escValue(provider)}">Send</button></div>`;
    return `<div class="provider-login"><p>1. <a href="${escValue(login.url)}" target="_blank" rel="noopener noreferrer">Open the sign-in page</a> and sign in with <b>this</b> account.</p>${step2}</div>`;
  }
  if (['starting', 'checking'].includes(login.state)) return `<p class="subtle"><span class="spinner" aria-hidden="true"></span> ${login.state === 'starting' ? 'Starting sign-in…' : 'Checking…'}</p>`;
  if (login.state === 'failed') return `<p class="form-status">Sign-in failed. ${esc(login.message || '')}</p>`;
  if (login.state === 'done') return '<p class="subtle">Signed in.</p>';
  return '';
}

function rowMarkup(provider, account, login) {
  const ref = `${provider}:${account.id}`;
  const limit = account.max_concurrent ? `${account.running}/${account.max_concurrent}` : `${account.running} (no limit)`;
  const extra = account.id === 'default';
  const credential = account.kind === 'key'
    ? (extra ? '' : `<div class="form-row"><input type="password" name="key-${escValue(ref)}" placeholder="${provider === 'claude' ? 'sk-ant-…' : 'sk-…'}" autocomplete="off"><button type="button" data-account-key="${escValue(ref)}">${account.key_saved ? 'Replace key' : 'Save key'}</button></div>`)
    : `<button type="button" data-account-login="${escValue(ref)}">${account.signed_in ? 'Sign in again' : 'Sign in'}</button>`;
  return `<tr><td><b>${esc(account.label)}</b>${extra ? ' <span class="subtle">(the first sign-in)</span>' : ` <span class="subtle">${esc(account.id)}</span>`}<div class="subtle">${esc(account.kind === 'key' ? 'API key' : 'Subscription')}${account.method ? ` · ${esc(account.method)}` : ''}${usage(account.usage) ? ` · ${esc(usage(account.usage))}` : ''}</div>${account.hold?.held ? `<div class="subtle">${esc(account.hold.reason || '')}</div>` : ''}${progressMarkup(login, provider, account.id)}</td><td>${stateMarkup(account)}</td><td><label class="inline">Runs at once <input type="number" min="0" max="20" value="${escValue(account.max_concurrent)}" name="limit-${escValue(ref)}" style="width:4em"></label><div class="subtle">now ${esc(limit)}</div><label class="inline" title="LAIka stops using this account at this share of its plan until the window resets. Lower it on the plan you also use yourself.">Stop at <input type="number" min="10" max="100" value="${escValue(account.stop_percent ?? 95)}" name="stop-${escValue(ref)}" style="width:4em">%</label><button type="button" data-account-limit="${escValue(ref)}">Save</button></td><td><div class="form-row">${extra && account.kind !== 'key' ? '' : credential}<button type="button" data-account-toggle="${escValue(ref)}" data-enabled="${account.enabled ? '1' : ''}">${account.enabled ? 'Turn off' : 'Turn on'}</button>${extra ? '' : `<button type="button" class="danger" data-account-remove="${escValue(ref)}">Remove</button>`}</div></td></tr>`;
}

export function accountsMarkup(data = {}) {
  const accounts = data.accounts || {};
  const sections = ['claude', 'codex'].map(provider => {
    const rows = (accounts[provider] || []).map(a => rowMarkup(provider, a, data.login?.[provider])).join('');
    return `<h4>${NAMES[provider]}</h4><div class="table-wrap"><table class="job-table"><thead><tr><th>Account</th><th>State</th><th>Limit</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }).join('');
  return `<h3>More accounts</h3><p class="subtle">Add more Claude or Codex accounts to get more done: each run goes to the account with the most room left, and when one hits its limit LAIka keeps going on the others (a provider pauses only when all of its accounts are). Check each provider's terms for using several accounts.</p>${sections}<form class="form-row" data-account-form><select name="provider"><option value="claude">Claude</option><option value="codex">Codex</option></select><input name="label" placeholder="Name, e.g. Second plan" maxlength="60" required><select name="kind"><option value="login">Subscription sign-in</option><option value="key">API key</option></select><button type="button" data-account-add>Add account</button><span class="form-status" data-account-status role="status"></span></form>`;
}

export const slug = label => (label || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24) || 'account';

let poll = null;

export async function loadAccounts(node) {
  try {
    const data = await requestJSON('/api/ai-providers');
    if (node.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return; // typing
    node.innerHTML = accountsMarkup(data);
    const signing = Object.values(data.login || {}).some(l => ['starting', 'waiting', 'checking'].includes(l?.state));
    clearTimeout(poll);
    if (signing) poll = setTimeout(() => node.isConnected && loadAccounts(node), 2500);
  } catch (error) {
    node.innerHTML = `<p class="subtle">${esc(error.message)}</p>`;
  }
}

function status(message) {
  const node = document.querySelector('[data-account-status]');
  if (node) node.textContent = message;
}

async function act(work) {
  const card = document.querySelector('[data-accounts-card]');
  try {
    await work();
  } catch (error) {
    status(error.message);
    return;
  }
  if (card) loadAccounts(card);
}

const split = ref => ref.split(':');
const path = ref => {
  const [provider, account] = split(ref);
  return `/api/ai-providers/${encodeURIComponent(provider)}/accounts/${encodeURIComponent(account)}`;
};

if (typeof document !== 'undefined') {
  window.addEventListener('laika:settings-loaded', () => {
    const node = document.querySelector('[data-accounts-card]');
    if (node) loadAccounts(node);
  });
  registerClick('accountAdd', button => act(async () => {
    const form = button.closest('form');
    const label = form.label.value.trim();
    if (!label) throw new Error('Give the account a name.');
    const provider = form.provider.value;
    await requestJSON(`/api/ai-providers/${provider}/accounts`, { method: 'POST', body: JSON.stringify({
      id: slug(label), label, kind: form.kind.value, max_concurrent: provider === 'claude' ? 2 : 0 }) });
  }));
  registerClick('accountLogin', button => act(() =>
    requestJSON(`${path(button.dataset.accountLogin)}/login`, { method: 'POST', body: JSON.stringify({ request_id: newRequestId() }) })));
  registerClick('accountCode', button => act(() =>
    requestJSON('/api/ai-providers/claude/code', { method: 'POST', body: JSON.stringify({ code: button.closest('div').querySelector('input').value.trim() }) })));
  registerClick('accountKey', button => act(() => {
    const input = document.querySelector(`input[name="key-${CSS.escape(button.dataset.accountKey)}"]`);
    return requestJSON(`${path(button.dataset.accountKey)}/key`, { method: 'PUT', body: JSON.stringify({ key: input.value.trim() }) });
  }));
  registerClick('accountLimit', button => act(() => {
    const ref = CSS.escape(button.dataset.accountLimit);
    const body = { max_concurrent: Number(document.querySelector(`input[name="limit-${ref}"]`).value) };
    const stop = document.querySelector(`input[name="stop-${ref}"]`);
    if (stop) body.stop_percent = Number(stop.value);
    return requestJSON(path(button.dataset.accountLimit), { method: 'PATCH', body: JSON.stringify(body) });
  }));
  registerClick('accountToggle', button => act(() =>
    requestJSON(path(button.dataset.accountToggle), { method: 'PATCH', body: JSON.stringify({ enabled: !button.dataset.enabled }) })));
  registerClick('accountRemove', button => act(() => {
    if (!window.confirm('Remove this account? LAIka stops using it and deletes its sign-in or key from the server.')) return null;
    return requestJSON(path(button.dataset.accountRemove), { method: 'DELETE' });
  }));
}
