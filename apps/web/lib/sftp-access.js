// Settings → SFTP access (everyone): how to connect an SFTP app to LAIka
// (services/sftp/laika_sftp.py) and the person's own SSH keys
// (apps/api/users_routes.py /api/sftp, /api/me/ssh-keys).
import { requestJSON } from './api.js';
import { esc, escValue } from './format.js';

const when = seconds => (seconds ? new Date(seconds * 1000).toLocaleDateString() : '');

export function sftpMarkup(info = {}, keys = [], host = globalThis.location?.hostname || 'this-server') {
  let status;
  if (!info.online) status = '<p class="notice">The SFTP service is not running on this server. An administrator can check it with <code>sudo laika doctor</code>.</p>';
  else if (!info.enabled) status = '<p class="notice">SFTP is switched off on this server (Settings → SFTP server).</p>';
  else status = '';
  const user = info.username || 'your-username';
  const address = `sftp://${user}@${host}:${info.port || 2222}`;
  const rows = keys.length
    ? keys.map(key => `<li class="ssh-key"><div><b>${esc(key.name)}</b> <span class="subtle">${esc(key.type)} · added ${esc(when(key.added))}</span><div class="subtle mono">${esc(key.fingerprint)}</div></div><button type="button" class="danger-button" data-ssh-remove="${escValue(key.id)}">Remove</button></li>`).join('')
    : '<li class="subtle">No keys yet: you sign in with your LAIka password.</li>';
  return `<div class="settings-card"><h3>Connect an SFTP app</h3>${status}
<p>Use WinSCP, FileZilla, Cyberduck, VS Code (an SFTP extension) or <code>sftp</code> with your LAIka username and password.</p>
<div class="sftp-facts"><span>Address</span><code class="mono">${esc(address)}</code><span>Host</span><code>${esc(host)}</code><span>Port</span><code>${esc(info.port || 2222)}</code><span>Username</span><code>${esc(user)}</code><span>Server key</span><code class="mono">${esc(info.fingerprint || 'unknown')}</code></div>
<p class="subtle">The first time you connect, your app shows the server key: it must match the one above.</p>
<h3>What you will see</h3>
<ul class="plain-list"><li>A folder for each project you can open, with <b>code</b> (main) and <b>data</b> (the app's data folder).</li>
<li><b>data</b> changes at once. <b>code</b> changes are collected and committed to main as <b>one commit by you</b> ${esc(info.commit_seconds || 30)} seconds after your last change, or when you disconnect. Until then only you see them.</li>
<li>If someone changed the same file on main meanwhile, yours is not forced over it: it is kept in App data/.laika-sftp-conflicts and you are notified.</li>
<li>With View access everything is read-only.</li></ul></div>
<form id="ssh-key-form" class="settings-card" autocomplete="off"><h3>Your SSH keys</h3><p class="subtle">Optional: sign in with a key instead of your password. Paste the public key (the <code>.pub</code> file), never the private one.</p><ul class="plain-list ssh-keys">${rows}</ul>
<label class="field">Name<input name="name" required maxlength="60" placeholder="e.g. Work laptop"></label>
<label class="field">Public key<textarea name="key" required rows="3" spellcheck="false" autocapitalize="off" placeholder="ssh-ed25519 AAAA… you@computer"></textarea></label>
<div class="settings-actions"><button type="submit" class="primary">Add key</button><span class="form-status" role="status"></span></div></form>`;
}

export async function sftpData() {
  const [info, keys] = await Promise.all([requestJSON('/api/sftp').catch(() => ({})), requestJSON('/api/me/ssh-keys').catch(() => ({ keys: [] }))]);
  return sftpMarkup(info, keys.keys || []);
}

if (typeof document !== 'undefined') {
  const redraw = () => window.dispatchEvent(new CustomEvent('laika:settings-redraw'));
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (form?.id !== 'ssh-key-form') return;
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    try {
      await requestJSON('/api/me/ssh-keys', { method: 'POST', body: JSON.stringify({ name: values.name.trim(), key: values.key.trim() }) });
      redraw();
    } catch (error) {
      form.querySelector('.form-status').textContent = error.message;
    }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-ssh-remove]');
    if (!button) return;
    if (!window.confirm('Remove this key? Apps using it can no longer sign in.')) return;
    try {
      await requestJSON(`/api/me/ssh-keys/${encodeURIComponent(button.dataset.sshRemove)}`, { method: 'DELETE' });
      redraw();
    } catch (error) {
      window.alert(error.message);
    }
  });
}
