// Settings → Remote access (administrators): Tailscale on this server, so you
// reach LAIka and its apps from your own devices anywhere without opening a
// port (apps/api/remote_routes.py, scripts/laika-remote.py on the host).
import { requestJSON, newRequestId } from './api.js';
import { esc, escValue } from './format.js';

const STATES = {
  not_installed: 'Not installed',
  needs_login: 'Installed, not connected to an account',
  stopped: 'Installed, stopped',
  starting: 'Starting',
  connected: 'Connected',
  unknown: 'Unknown'
};

export function remoteMarkup(data = {}, appPorts = []) {
  const s = data.status || {};
  const progress = data.progress || {};
  const login = { ...(data.login || {}) };
  // A sign-in link is good for 10 minutes (scripts/laika-remote.py).
  if (login.state === 'waiting' && login.at && Date.now() / 1000 - login.at > 600) Object.assign(login, { state: 'failed', message: 'The sign-in link expired. Try again.' });
  const state = s.installed === false ? 'not_installed' : s.state || 'unknown';
  const busy = progress.state === 'installing' || login.state === 'starting' || login.state === 'waiting';
  const warnings = [
    s.funnel && 'Tailscale Funnel is ON: it publishes a service from this server to the whole internet. Turn it off: sudo tailscale funnel reset',
    s.serve && 'Tailscale Serve is configured on this server; LAIka does not use it. Check: sudo tailscale serve status',
    s.ssh && 'Tailscale SSH is on: anyone your tailnet allows can get a shell here. Turn it off: sudo tailscale set --ssh=false'
  ].filter(Boolean).map(text => `<p class="notice warn">${esc(text)}</p>`).join('');
  let action;
  if (state === 'not_installed') action = `<button type="button" class="primary" data-remote-action="install"${busy ? ' disabled' : ''}>Install Tailscale</button>`;
  else if (state === 'connected') action = `<button type="button" class="danger-button" data-remote-action="logout">Disconnect</button>`;
  else action = `<button type="button" class="primary" data-remote-action="login"${busy ? ' disabled' : ''}>Connect to my Tailscale account</button>`;
  const step = progress.state === 'installing' || progress.state === 'failed'
    ? `<p class="${progress.state === 'failed' ? 'form-status' : 'subtle'}">${progress.state === 'installing' ? '<span class="spinner" aria-hidden="true"></span> ' : ''}${esc(progress.message || '')}</p>`
    : '';
  const signIn = login.state === 'waiting' && login.url
    ? `<div class="notice"><p><b>Approve this server in Tailscale:</b> open the link, sign in to your Tailscale account and connect the device. This page updates by itself.</p><p><a class="button primary" href="${escValue(login.url)}" target="_blank" rel="noopener">Open the Tailscale sign-in</a></p></div>`
    : login.state === 'starting'
      ? '<p class="subtle"><span class="spinner" aria-hidden="true"></span> Asking Tailscale for a sign-in link…</p>'
      : login.state === 'failed' || login.state === 'done'
        ? `<p class="${login.state === 'failed' ? 'form-status' : 'subtle'}">${esc(login.message || '')}</p>`
        : '';
  const host = s.dns_name || (s.ips || [])[0] || '';
  const ip = (s.ips || []).find(address => address.includes('.')) || '';
  const addresses = state === 'connected' && host
    ? `<h3>Your addresses on the tailnet</h3><div class="sftp-facts"><span>Dashboard</span><code>http://${esc(s.dns_name && s.magic_dns ? s.dns_name : ip)}:8080</code>${ip && s.dns_name && s.magic_dns ? `<span>or</span><code>http://${esc(ip)}:8080</code>` : ''}${appPorts.map(([name, port]) => `<span>${esc(name)}</span><code>http://${esc(s.dns_name && s.magic_dns ? s.dns_name : ip)}:${esc(port)}</code>`).join('')}<span>SFTP</span><code>${esc(s.dns_name && s.magic_dns ? s.dns_name : ip)} port 2222</code></div><p class="subtle">Install Tailscale on your phone or laptop and sign in with the same account; then these addresses work from anywhere.${s.magic_dns ? '' : ' (Turn on MagicDNS in the Tailscale admin console to use the name instead of the number.)'}</p>`
    : '';
  return `<div class="settings-card"><h3>Tailscale</h3><p class="subtle">Reach LAIka, its apps and SFTP from your own devices anywhere, through your private Tailscale network. No port is opened to the internet, and nothing here becomes public.</p><div class="sftp-facts"><span>State</span><span><b>${esc(STATES[state] || state)}</b>${s.version ? ` <span class="subtle">· ${esc(s.version)}</span>` : ''}</span>${s.tailnet ? `<span>Tailnet</span><span>${esc(s.tailnet)}</span>` : ''}${host ? `<span>This server</span><code>${esc(host)}</code>` : ''}</div>${warnings}${step}${signIn}<div class="settings-actions">${action}<button type="button" data-remote-action="refresh">Refresh</button></div>${addresses}</div>`;
}

let timer = null;

export async function remoteData() {
  const [data, projects] = await Promise.all([requestJSON('/api/remote'), requestJSON('/api/projects').catch(() => [])]);
  if (data.stale) post('refresh').catch(() => {});
  const ports = (Array.isArray(projects) ? projects : []).filter(p => p.app?.port && p.app?.state === 'running').map(p => [p.name || p.id, p.app.port]);
  const busy = data.progress?.state === 'installing' || ['starting', 'waiting'].includes(data.login?.state) || data.stale;
  clearTimeout(timer);
  if (busy) timer = setTimeout(() => window.dispatchEvent(new CustomEvent('laika:settings-redraw')), 2500);
  return remoteMarkup(data, ports);
}

const post = action => requestJSON(`/api/remote/${action}`, { method: 'POST', body: JSON.stringify({ request_id: newRequestId() }) });

if (typeof document !== 'undefined') {
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-remote-action]');
    if (!button) return;
    const action = button.dataset.remoteAction;
    if (action === 'logout' && !window.confirm('Disconnect this server from Tailscale? Your devices can no longer reach it through the tailnet.')) return;
    button.disabled = true;
    try {
      await post(action);
    } catch (error) {
      window.alert(error.message);
    }
    setTimeout(() => window.dispatchEvent(new CustomEvent('laika:settings-redraw')), 1200);
  });
}
