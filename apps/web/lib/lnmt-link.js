// LNMT, the web hosting panel next to LAIka (apps/api/link_routes.py):
// Settings → System connects it (paste LNMT's pairing code); a project's
// Settings can deploy it to one of your LNMT apps; and #/connect is where
// LNMT's "Sign in with LAIka" lands: you confirm, and LNMT signs you in.
import { newRequestId, requestJSON } from './api.js';
import { currentMe, loadMe } from './access.js';
import { esc } from './format.js';
import { registerClick, registerPanel } from './registry.js';

export function lnmtCardMarkup(status = {}) {
  if (status.connected) {
    return `<h3>LNMT (web hosting)</h3><p>Connected to <b>${esc(status.name || 'LNMT')}</b> at <code>${esc(status.url)}</code>.</p>
      <p class="subtle">People sign in to LNMT with their LAIka account and appear there by themselves; LNMT can start projects here for their sites, and projects can deploy to LNMT apps.</p>
      <div class="form-row"><button type="button" data-lnmt-disconnect>Disconnect</button><span class="form-status" data-lnmt-status role="status"></span></div>`;
  }
  return `<h3>LNMT (web hosting)</h3><p class="subtle">Connect LNMT so people sign in there with their LAIka account, LNMT can have LAIka build sites, and projects can deploy to LNMT apps. In LNMT: System → LAIka → Make a pairing code, then paste it here.</p>
    <form class="form-row" data-lnmt-connect><input name="code" placeholder="pairing code" autocomplete="off" required><button type="submit">Connect</button><span class="form-status" data-lnmt-status role="status"></span></form>`;
}

export function deployCardMarkup(projectId, info = {}, apps = null) {
  if (!info.connected) return '';
  const target = info.target;
  const current = target?.app ? `<p>Deploys to <b>${esc(target.app)}</b> on LNMT: every approved change is pushed there, built and put live.</p>`
    : target?.git_url ? '<p>Deploys to its LNMT app (set up from LNMT): every approved change is pushed there, built and put live.</p>'
      : '<p class="subtle">Pick one of your LNMT apps built from git (kind “Your own Dockerfile” or “WordPress site built by LAIka”): every approved change of this project is pushed there, built and put live.</p>';
  const choices = Array.isArray(apps) ? apps.filter(a => a.git_url) : [];
  const picker = apps === null ? `<button type="button" data-lnmt-apps>${target?.app || target?.git_url ? 'Change…' : 'Choose an LNMT app…'}</button>`
    : choices.length ? `<select name="target">${choices.map(a => `<option value="${esc(`${a.customer}/${a.name}`)}">${esc(a.name)}${a.sites?.length ? ` (${esc(a.sites.join(', '))})` : ''}</option>`).join('')}</select><button type="submit">Deploy there</button>`
      : '<span class="subtle">You have no LNMT apps built from git yet: create one in LNMT first.</span>';
  return `<h3>Deploy to LNMT</h3>${current}<form class="form-row" data-lnmt-deploy-form="${esc(projectId)}">${picker}<span class="form-status" data-lnmt-deploy-status role="status"></span></form>`;
}

export const connectDialogMarkup = (name, service) => `<div class="lnmt-connect" role="dialog" aria-modal="true" aria-labelledby="lnmt-connect-title"><div class="lnmt-connect-card">
  <h2 id="lnmt-connect-title">Sign in to ${esc(service || 'LNMT')}</h2>
  <p>${esc(service || 'LNMT')} asks to sign you in as <b>${esc(name)}</b>. It learns your user name and role here, nothing else.</p>
  <div class="form-row"><button type="button" class="primary" data-lnmt-allow>Continue as ${esc(name)}</button><button type="button" data-lnmt-deny>Cancel</button></div>
  <span class="form-status" data-lnmt-status role="status"></span></div></div>`;

export const connectState = hash => {
  const match = /^#\/connect\?(?:.*&)?state=([A-Za-z0-9_-]{16,128})/.exec(String(hash || ''));
  return match ? match[1] : null;
};

if (typeof document !== 'undefined') {
  const say = message => document.querySelectorAll('[data-lnmt-status]').forEach(node => { node.textContent = message; });

  window.addEventListener('laika:settings-loaded', async () => {
    const body = document.querySelector('.settings-body');
    if (!body || !document.querySelector('[data-backups-card]')) return;  // the System section
    const card = document.createElement('div');
    card.className = 'settings-card';
    card.dataset.lnmtCard = '';
    body.appendChild(card);
    try { card.innerHTML = lnmtCardMarkup(await requestJSON('/api/link/lnmt')); } catch (error) { card.innerHTML = `<p class="subtle">${esc(error.message)}</p>`; }
  });

  document.addEventListener('submit', async event => {
    const connect = event.target.closest?.('[data-lnmt-connect]');
    const deploy = event.target.closest?.('[data-lnmt-deploy-form]');
    if (!connect && !deploy) return;
    event.preventDefault();
    if (connect) {
      say('Connecting…');
      try {
        const status = await requestJSON('/api/link/lnmt', { method: 'POST', body: JSON.stringify({ code: connect.code.value.trim() }) });
        document.querySelector('[data-lnmt-card]').innerHTML = lnmtCardMarkup(status);
      } catch (error) { say(error.message); }
      return;
    }
    const [customer, app] = String(deploy.target?.value || '').split('/');
    const status = deploy.querySelector('[data-lnmt-deploy-status]');
    status.textContent = 'Setting up the push and LNMT’s deploy key…';
    try {
      const project = deploy.dataset.lnmtDeployForm;
      await requestJSON(`/api/projects/${encodeURIComponent(project)}/deploy-lnmt`, { method: 'POST', body: JSON.stringify({ customer, app, request_id: newRequestId().slice(0, 40) }) });
      deploy.closest('[data-lnmt-deploy]').innerHTML = deployCardMarkup(project, await requestJSON(`/api/projects/${encodeURIComponent(project)}/deploy-lnmt`));
    } catch (error) { status.textContent = error.message; }
  });

  registerClick('lnmtDisconnect', async () => {
    if (!confirm('Disconnect LNMT? People can no longer sign in there with LAIka; projects keep pushing to their apps.')) return;
    try { document.querySelector('[data-lnmt-card]').innerHTML = lnmtCardMarkup(await requestJSON('/api/link/lnmt', { method: 'DELETE' })); } catch (error) { say(error.message); }
  });
  registerClick('lnmtApps', async button => {
    const node = button.closest('[data-lnmt-deploy]');
    try {
      const apps = (await requestJSON('/api/link/lnmt/apps')).apps || [];
      node.innerHTML = deployCardMarkup(node.dataset.lnmtDeploy, await requestJSON(`/api/projects/${encodeURIComponent(node.dataset.lnmtDeploy)}/deploy-lnmt`), apps);
    } catch (error) { button.insertAdjacentText('afterend', ` ${error.message}`); }
  });
  registerClick('lnmtAllow', async () => {
    say('Signing you in…');
    try {
      const answer = await requestJSON('/api/connect/authorize', { method: 'POST', body: JSON.stringify({ state: connectState(location.hash) }) });
      location.href = answer.redirect;
    } catch (error) { say(error.message); }
  });
  registerClick('lnmtDeny', () => { document.querySelector('.lnmt-connect')?.remove(); location.hash = '#/'; });

  // Project Settings: fill the "Deploy to LNMT" card once per page.
  // #/connect?state=…: once signed in, ask before signing in to LNMT.
  registerPanel(async () => {
    document.querySelectorAll('[data-lnmt-deploy]:not([data-loaded])').forEach(async node => {
      node.dataset.loaded = '1';
      try { node.innerHTML = deployCardMarkup(node.dataset.lnmtDeploy, await requestJSON(`/api/projects/${encodeURIComponent(node.dataset.lnmtDeploy)}/deploy-lnmt`)); } catch { node.innerHTML = ''; }
    });
    if (!connectState(location.hash) || document.querySelector('.lnmt-connect')) return;
    const me = currentMe() || await loadMe();
    if (!me?.username) return;
    let service = 'LNMT';
    try { service = (await requestJSON('/api/link/lnmt')).name || service; } catch {}
    if (document.querySelector('.lnmt-connect')) return;
    const holder = document.createElement('div');
    holder.innerHTML = connectDialogMarkup(me.username, service);
    document.body.appendChild(holder.firstElementChild);
  });
}
