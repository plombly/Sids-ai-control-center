// Settings page (#/settings): where notifications go, what each event does,
// quiet hours and the weekly digest (apps/api/notify_routes.py). The server's
// channel (administrators) and everyone's own (My notifications, 1.2) share
// the same forms; KINDS says which endpoints and element ids each uses.
import { requestJSON } from './api.js';
import { esc, escValue } from './format.js';
import { onRoute } from './registry.js';
import { loadSettings, navMarkup, sectionMarkup, systemMarkup } from './system-settings.js';
import { accessData } from './auth.js';
import { isAdmin, loadMe, currentMe } from './access.js';
import { usersData } from './users.js';
import { MEMBER_SECTIONS } from './system-settings.js';
import { providerCard } from './setup-wizard.js';

export const KINDS = {
  server: { base: '/api/notifications', targets: 'notify-targets-form', rules: 'notify-rules-form', targetsStatus: 'notify-targets-status', rulesStatus: 'notify-rules-status' },
  mine: { base: '/api/me/notifications', targets: 'my-targets-form', rules: 'my-rules-form', targetsStatus: 'my-targets-status', rulesStatus: 'my-rules-status' }
};
const kindOfForm = id => Object.keys(KINDS).find(kind => KINDS[kind].targets === id || KINDS[kind].rules === id);
const MODE_LABELS = [['ping', 'Post + ping me'], ['post', 'Post'], ['off', 'Off']];
const DAY_LABELS = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };

export function targetsMarkup(targets = {}, kind = 'server') {
  const k = KINDS[kind];
  const mine = kind === 'mine';
  const state = (set, hint) => (set ? `<span class="target-set">Set ${esc(hint)}</span>` : '<span class="subtle">Not set</span>');
  const intro = mine
    ? 'Your own Discord webhook or ntfy topic. You hear about the projects you can see: approvals if you may approve, stuck jobs and app problems if you may build, and your goals. Saved on the server and never shown again.'
    : "The administrators' channel: every event, backups and health. Saved on the server and never shown again; type a new value to replace one, or remove it.";
  return `<form id="${k.targets}" class="settings-card" autocomplete="off" data-kind="${kind}"><h3>Where ${mine ? 'your' : 'server'} notifications go</h3><p class="subtle">${esc(intro)}</p>
<div class="settings-row"><label for="${kind}-discord">Discord webhook</label>${state(targets.discord, targets.discord_hint)}<input id="${kind}-discord" name="discord_webhook" type="password" placeholder="https://discord.com/api/webhooks/…" autocomplete="new-password">${targets.discord ? `<button type="button" class="detail-button" data-kind="${kind}" data-target-clear="discord_webhook">Remove</button>` : ''}</div>
<div class="settings-row"><label for="${kind}-mention">Discord user to ping</label><span class="subtle">${targets.mention ? esc(targets.mention) : 'Not set'}</span><input id="${kind}-mention" name="discord_mention" inputmode="numeric" placeholder="Your user ID (Developer Mode → Copy User ID)"></div>
<div class="settings-row"><label for="${kind}-ntfy">ntfy topic (optional)</label>${state(targets.ntfy, targets.ntfy_hint)}<input id="${kind}-ntfy" name="ntfy_url" type="password" placeholder="https://ntfy.sh/your-topic" autocomplete="new-password">${targets.ntfy ? `<button type="button" class="detail-button" data-kind="${kind}" data-target-clear="ntfy_url">Remove</button>` : ''}</div>
${mine ? '' : `<div class="settings-row"><label for="nt-dash">Dashboard address in messages</label><span class="subtle">${esc(targets.dashboard_url || 'automatic')}</span><input id="nt-dash" name="dashboard_url" placeholder="http://192.168.1.20:8080"></div>`}
<div class="settings-actions"><button type="submit">Save</button><button type="button" data-notify-test="${kind}">Send test</button><span id="${k.targetsStatus}" class="form-status" role="status"></span></div></form>`;
}

export function rulesMarkup(data, kind = 'server') {
  const k = KINDS[kind];
  const settings = data.settings || {};
  const events = (data.events || [])
    .map(event => {
      const current = settings.events?.[event.type] || 'post';
      const options = MODE_LABELS.map(([value, label]) => `<option value="${value}"${value === current ? ' selected' : ''}>${label}</option>`).join('');
      return `<div class="rule-row"><span>${esc(event.label)}${event.urgent ? ' <span class="subtle">(even in quiet hours)</span>' : ''}</span><select name="event:${escValue(event.type)}" aria-label="${escValue(event.label)}">${options}</select></div>`;
    })
    .join('');
  const quiet = settings.quiet || {};
  const digest = settings.digest || {};
  const days = (data.days || Object.keys(DAY_LABELS))
    .map(day => `<option value="${day}"${day === digest.day ? ' selected' : ''}>${DAY_LABELS[day] || day}</option>`)
    .join('');
  const goals = kind === 'mine'
    ? `<div class="rule-row"><span>Goals I hear about</span><select name="goals" aria-label="Goals I hear about"><option value="mine"${settings.goals !== 'all' ? ' selected' : ''}>Only the ones I gave</option><option value="all"${settings.goals === 'all' ? ' selected' : ''}>Every goal in my projects</option></select></div>`
    : '';
  return `<form id="${k.rules}" class="settings-card" data-kind="${kind}"><h3>What to send</h3>${events}${goals}
<h3>Quiet hours</h3><div class="settings-row"><label class="check"><input type="checkbox" name="quiet_enabled"${quiet.enabled ? ' checked' : ''}> Hold messages overnight</label><span class="time-range">from <input type="time" name="quiet_start" value="${escValue(quiet.start || '22:00')}"> to <input type="time" name="quiet_end" value="${escValue(quiet.end || '07:00')}"></span></div><p class="subtle">Held messages go out when quiet hours end. Failed backups and red health always go out.</p>
<h3>Weekly digest</h3><div class="settings-row"><span>Send it every</span><select name="digest_day">${days}</select><span>at</span><input type="time" name="digest_time" value="${escValue(digest.time || '18:00')}"></div>
<div class="settings-actions"><button type="submit">Save</button><button type="button" data-digest-preview="${kind}">Preview digest</button><span id="${k.rulesStatus}" class="form-status" role="status"></span></div><pre class="digest-preview" hidden></pre></form>`;
}

export function rulesFromForm(form, eventTypes) {
  const values = new FormData(form);
  const events = {};
  for (const type of eventTypes) events[type] = values.get(`event:${type}`) || 'post';
  const goals = values.get('goals');
  return {
    events,
    ...(goals ? { goals } : {}),
    quiet: { enabled: values.get('quiet_enabled') === 'on', start: values.get('quiet_start') || '22:00', end: values.get('quiet_end') || '07:00' },
    digest: { day: values.get('digest_day') || 'sun', time: values.get('digest_time') || '18:00' }
  };
}

let data = null;
const root = () => (typeof document === 'undefined' ? null : document.getElementById('settings-root'));
const say = (id, message) => {
  const node = document.getElementById(id);
  if (node) node.textContent = message;
};

let currentSection = 'general';

async function load(section = currentSection) {
  const container = root();
  if (!container) return;
  if (!currentMe()) await loadMe();
  const admin = isAdmin();
  if (!admin && !MEMBER_SECTIONS.includes(section)) section = 'access';
  currentSection = section;
  try {
    // The server-wide settings are the administrators'; members never load them.
    const settings = admin ? await loadSettings(true) : null;
    let body;
    if (section === 'users') {
      body = await usersData();
    } else if (section === 'notifications') {
      data = await requestJSON('/api/notifications');
      body = `${targetsMarkup(data.targets)}${rulesMarkup(data)}`;
    } else if (section === 'my-notifications') {
      try {
        data = await requestJSON('/api/me/notifications');
        body = `${targetsMarkup(data.targets, 'mine')}${rulesMarkup(data, 'mine')}`;
      } catch (error) {
        body = `<div class="settings-card"><p class="subtle">${esc(error.message)}</p></div>`;
      }
    } else if (section === 'phones') {
      body = '<section class="settings-section" id="devices-section"></section>';
    } else if (section === 'ai') {
      const providers = await requestJSON('/api/ai-providers').catch(() => ({}));
      window.dispatchEvent(new CustomEvent('laika:providers-loaded', { detail: providers }));
      body = `<div class="settings-card" id="setup-root-ai"><h3>Accounts</h3>${providerCard('claude', 'Claude', providers.status?.claude, providers.login?.claude, providers.keys?.anthropic_api_key)}${providerCard('codex', 'Codex', providers.status?.codex, providers.login?.codex, providers.keys?.openai_api_key)}<p class="subtle">Sign-in steps open in the setup guide: <a href="#/setup">run setup again</a>.</p></div>${sectionMarkup(settings, section, settings.values, settings.pending)}`;
    } else if (section === 'workers') {
      body = `<div class="settings-card"><h3>Right now</h3><div data-worker-scale="log"></div></div>${sectionMarkup(settings, section, settings.values, settings.pending)}`;
    } else if (section === 'access') {
      body = await accessData();
    } else if (section === 'system') {
      const [info, update] = await Promise.all([
        requestJSON('/api/app/info').catch(() => ({})),
        requestJSON('/api/system/update').catch(() => null)
      ]);
      body = systemMarkup({ version: info.version, commit: info.laika_commit, server_name: info.server_name }, update);
    } else {
      body = sectionMarkup(settings, section, settings.values, settings.pending) || sectionMarkup(settings, 'general', settings.values, settings.pending);
    }
    container.innerHTML = `<div class="settings-page settings-layout"><div class="page-head"><h2>Settings</h2></div>${navMarkup(settings, section, admin)}<div class="settings-body">${body}</div></div>`;
    if (section === 'workers') window.dispatchEvent(new CustomEvent('laika:worker-scale-refresh'));
    window.dispatchEvent(new CustomEvent('laika:settings-loaded'));
  } catch (error) {
    container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('submit', async event => {
    const form = event.target;
    const kind = kindOfForm(form.id);
    if (!kind) return;
    const k = KINDS[kind];
    if (form.id === k.targets) {
      event.preventDefault();
      const body = {};
      for (const [key, value] of new FormData(form).entries()) if (String(value).trim()) body[key] = String(value).trim();
      if (!Object.keys(body).length) return say(k.targetsStatus, 'Nothing to save');
      try {
        await requestJSON(`${k.base}/targets`, { method: 'PUT', body: JSON.stringify(body) });
        await load();
        say(k.targetsStatus, 'Saved');
      } catch (error) {
        say(k.targetsStatus, error.message);
      }
    } else {
      event.preventDefault();
      try {
        await requestJSON(`${k.base}/settings`, {
          method: 'PUT',
          body: JSON.stringify(rulesFromForm(form, (data?.events || []).map(item => item.type)))
        });
        say(k.rulesStatus, 'Saved');
      } catch (error) {
        say(k.rulesStatus, error.message);
      }
    }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button || !root()?.contains(button)) return;
    const k = KINDS[button.dataset.notifyTest || button.dataset.digestPreview || button.dataset.kind] || KINDS.server;
    if (button.dataset.notifyTest !== undefined) {
      say(k.targetsStatus, 'Sending…');
      try {
        await requestJSON(`${k.base}/test`, { method: 'POST' });
        say(k.targetsStatus, 'Sent. Check Discord or ntfy.');
      } catch (error) {
        say(k.targetsStatus, error.message);
      }
    } else if (button.dataset.targetClear) {
      if (!globalThis.confirm?.('Remove this? Notifications stop going there.')) return;
      try {
        await requestJSON(`${k.base}/targets`, { method: 'PUT', body: JSON.stringify({ [button.dataset.targetClear]: '' }) });
        await load();
      } catch (error) {
        say(k.targetsStatus, error.message);
      }
    } else if (button.dataset.digestPreview !== undefined) {
      const pre = button.closest('form')?.querySelector('.digest-preview');
      try {
        const preview = await requestJSON(`${k.base}/digest-preview`);
        pre.textContent = `${preview.title}\n\n${preview.text}`;
        pre.hidden = false;
      } catch (error) {
        say(k.rulesStatus, error.message);
      }
    }
  });
}

onRoute(route => {
  if (route.view === 'settings') load(route.section || 'general');
});
if (typeof window !== 'undefined') window.addEventListener('laika:settings-redraw', () => load());
