// First-run setup (#/setup), right after the administrator account exists:
// name and look, AI providers (subscription sign-in or API keys), capacity,
// notifications, backups, a network safety check, then done. Every step can
// be skipped and changed later in Settings.
import { requestJSON, newRequestId } from './api.js';
import { esc, escValue } from './format.js';
import { onRoute } from './registry.js';

export const STEPS = [
  ['welcome', 'Name & look'],
  ['ai', 'AI providers'],
  ['capacity', 'Capacity'],
  ['notify', 'Notifications'],
  ['backups', 'Backups'],
  ['safety', 'Safety check'],
  ['done', 'Done']
];

const opt = value => (value || value === 0 ? esc(value) : '');

export function stepsMarkup(current) {
  const index = STEPS.findIndex(([id]) => id === current);
  return `<ol class="setup-steps">${STEPS.map(([id, label], i) => `<li class="${i < index ? 'done' : i === index ? 'current' : ''}">${esc(label)}</li>`).join('')}</ol>`;
}

const field = (label, name, value, extra = '') =>
  `<label class="field">${esc(label)}<input name="${escValue(name)}" value="${escValue(value)}"${extra}></label>`;

export function providerCard(name, label, info = {}, login = null, keySaved = false) {
  const status = info?.signed_in
    ? `<span class="pill ok">Signed in${info.plan ? ` · ${opt(info.plan)}` : info.method ? ` · ${opt(info.method)}` : ''}</span>`
    : info?.installed === false
      ? '<span class="pill bad">Not installed</span>'
      : '<span class="pill warn">Not signed in</span>';
  let progress = '';
  if (login?.state === 'waiting' && login.url) {
    progress = `<div class="provider-login"><p>1. <a href="${escValue(login.url)}" target="_blank" rel="noopener noreferrer">Open the sign-in page</a></p>${
      login.code ? `<p>2. Enter this code there: <b class="login-code">${esc(login.code)}</b></p>` : `<p>2. Paste the code it shows you:</p><div class="form-row"><input name="claude_code" placeholder="Code from Claude" autocomplete="off"><button type="button" data-claude-code>Send</button></div>`
    }<p class="subtle">${opt(login.message)}</p></div>`;
  } else if (login && ['starting', 'checking'].includes(login.state)) {
    progress = `<p class="subtle"><span class="spinner" aria-hidden="true"></span> ${login.state === 'starting' ? 'Starting sign-in…' : 'Checking…'}</p>`;
  } else if (login?.state === 'failed') {
    progress = `<p class="form-status">Sign-in failed. ${opt(login.message)}</p>`;
  }
  const keyName = name === 'claude' ? 'anthropic_api_key' : 'openai_api_key';
  const keyHint = name === 'claude' ? 'sk-ant-…' : 'sk-…';
  return `<div class="provider-card" data-provider="${escValue(name)}"><div class="item-head"><h3>${esc(label)}</h3>${status}</div><p class="subtle">${
    name === 'claude' ? 'Plans goals, reviews every change and repairs what review finds. A Claude Pro or Max subscription works, or an Anthropic API key.' : 'Writes most of the code. A ChatGPT plan works, or an OpenAI API key.'
  }</p><div class="form-row"><button type="button" data-provider-login="${escValue(name)}">Sign in with your ${name === 'claude' ? 'Claude' : 'ChatGPT'} account</button></div>${progress}<details><summary>Use an API key instead${keySaved ? ' (saved)' : ''}</summary><div class="form-row"><input type="password" name="${keyName}" placeholder="${keyHint}" autocomplete="off"><button type="button" data-provider-key="${keyName}">Save key</button></div><p class="subtle">Keys are stored on the server, readable only by LAIka, and never shown again.</p></details></div>`;
}

export function stepMarkup(step, data = {}) {
  const values = data.values || {};
  if (step === 'welcome') {
    const accents = ['#5b9dff', '#3fcf8e', '#b48cff', '#ff9f5a', '#ef5d6c', '#3cc8c8'];
    return `<h2>Let's set up <span class="wordmark">L<span class="wordmark-ai">AI</span>ka</span></h2><p class="subtle">A few choices, then you can start building. Everything here can be changed later in Settings.</p>${field('What should this server be called?', 'SERVER_NAME', values.SERVER_NAME || 'LAIka', ' maxlength="60"')}<label class="field">Theme<select name="THEME">${['system', 'dark', 'light'].map(theme => `<option value="${theme}"${values.THEME === theme ? ' selected' : ''}>${theme === 'system' ? 'Follow this device' : theme[0].toUpperCase() + theme.slice(1)}</option>`).join('')}</select></label><div class="field">Accent colour<div class="color-setting">${accents
      .map(color => `<label class="swatch-pick"><input type="radio" name="ACCENT" value="${color}"${(values.ACCENT || '#5b9dff') === color ? ' checked' : ''}><span class="swatch" style="background:${color}"></span></label>`)
      .join('')}</div></div>`;
  }
  if (step === 'ai') {
    const status = data.providers?.status || {};
    const login = data.providers?.login || {};
    const keys = data.providers?.keys || {};
    return `<h2>Connect your AI</h2><p class="subtle">LAIka uses Claude and Codex (OpenAI). One is enough to start; both work best.</p>${providerCard('claude', 'Claude', status.claude, login.claude, keys.anthropic_api_key)}${providerCard('codex', 'Codex', status.codex, login.codex, keys.openai_api_key)}<button type="button" class="detail-button" data-provider-refresh>Check again</button>`;
  }
  if (step === 'capacity') {
    const info = data.info || {};
    return `<h2>How much at once?</h2><p class="subtle">This server has ${esc(info.cpus || '?')} CPUs and ${esc(info.memory_gb || '?')} GB of memory. Each worker runs one job at a time.</p>${field('Workers', 'WORKER_COUNT', values.WORKER_COUNT || info.suggested_workers || 4, ' type="number" min="1" max="32"')}<span class="field-hint">Suggested for this server: ${esc(info.suggested_workers || 4)}.</span>${field('Claude runs at the same time', 'CLAUDE_MAX_CONCURRENT', values.CLAUDE_MAX_CONCURRENT || 2, ' type="number" min="1" max="16"')}<span class="field-hint">Subscriptions have usage limits; fewer parallel Claude runs last longer.</span>`;
  }
  if (step === 'notify') {
    return `<h2>Notifications (optional)</h2><p class="subtle">LAIka can tell you when something is ready for approval or needs you.</p>${field('Discord webhook', 'discord_webhook', '', ' type="password" placeholder="https://discord.com/api/webhooks/…" autocomplete="off"')}${field('ntfy topic', 'ntfy_url', '', ' type="password" placeholder="https://ntfy.sh/your-topic" autocomplete="off"')}<button type="button" class="detail-button" data-setup-test-notify>Send a test</button><span class="form-status" id="setup-notify-status" role="status"></span>`;
  }
  if (step === 'backups') {
    return `<h2>Backups</h2><p class="subtle">Every day LAIka saves its data, your projects and its settings on this server.</p>${field('Daily backup at', 'BACKUP_TIME', values.BACKUP_TIME || '03:30', ' type="time"')}${field('Backups to keep', 'BACKUP_KEEP', values.BACKUP_KEEP || 14, ' type="number" min="1" max="365"')}${field('Off-site copy (optional rsync target)', 'BACKUP_REMOTE', values.BACKUP_REMOTE || '', ' placeholder="backup@nas:/backups/laika"')}`;
  }
  if (step === 'safety') {
    const exposed = data.info?.public_addresses || [];
    return `<h2>Keep it private</h2><p>LAIka can write and run code on this server. It is built for your own network or VPN, <b>never the public internet</b>, and is not supported there.</p><ul class="setup-list"><li>Reach it on your LAN, or from outside through your VPN (OpenVPN, WireGuard, Tailscale…).</li><li>Do not forward port 8080 on your router or put it behind a public proxy.</li><li>Phones use the LAIka app with their own revocable keys (Settings → Phones &amp; apps).</li></ul>${
      exposed.length
        ? `<div class="notice warn-notice"><b>This server has a public address (${esc(exposed.join(', '))}).</b> Make sure a firewall blocks port 8080 from the internet.</div>`
        : '<div class="notice"><b>Looks private:</b> this server has no public addresses.</div>'
    }`;
  }
  return `<h2>You're all set</h2><p>LAIka is ready. Start with a project: an empty one, or import a repository from GitHub.</p><div class="form-row"><a class="button primary" href="#/projects/new" data-setup-finish>Create a project</a><a class="button" href="#/settings/phones" data-setup-finish>Pair a phone</a><a class="button" href="#/" data-setup-finish>Go to the dashboard</a></div>`;
}

export function wizardMarkup(step, data) {
  const index = STEPS.findIndex(([id]) => id === step);
  const nav = step === 'done' ? '' : `<div class="wizard-actions">${index > 0 ? '<button type="button" data-setup-back>Back</button>' : ''}<button type="button" class="detail-button" data-setup-skip>Skip</button><button type="submit" class="primary">Continue</button></div>`;
  return `<section class="panel setup-panel">${stepsMarkup(step)}<form id="setup-form" data-step="${escValue(step)}" autocomplete="off">${stepMarkup(step, data)}<span class="form-status" role="status"></span>${nav}</form></section>`;
}

// Settings changes a step makes (only the settings keys).
export function stepChanges(step, values) {
  const pick = keys => Object.fromEntries(keys.filter(key => values[key] !== undefined && values[key] !== '').map(key => [key, values[key]]));
  if (step === 'welcome') return pick(['SERVER_NAME', 'THEME', 'ACCENT']);
  if (step === 'capacity') return pick(['WORKER_COUNT', 'CLAUDE_MAX_CONCURRENT']);
  if (step === 'backups') return { ...pick(['BACKUP_TIME', 'BACKUP_KEEP']), BACKUP_REMOTE: values.BACKUP_REMOTE || '' };
  return {};
}

if (typeof document !== 'undefined') {
  let step = 'welcome';
  let data = {};
  let poller = null;
  const root = () => document.getElementById('setup-root');
  const say = message => {
    const node = root()?.querySelector('#setup-form > .form-status');
    if (node) node.textContent = message;
  };
  async function load() {
    const [settings, providers, info] = await Promise.all([
      requestJSON('/api/settings').catch(() => ({})),
      requestJSON('/api/ai-providers').catch(() => ({})),
      requestJSON('/api/setup/info').catch(() => ({}))
    ]);
    data = { values: settings.values || {}, providers, info };
  }
  async function draw() {
    const node = root();
    if (!node) return;
    node.innerHTML = wizardMarkup(step, data);
    clearTimeout(poller);
    const busy = Object.values(data.providers?.login || {}).some(login => login && ['starting', 'waiting', 'checking'].includes(login.state));
    if (step === 'ai' && busy) {
      poller = setTimeout(async () => {
        data.providers = await requestJSON('/api/ai-providers').catch(() => data.providers);
        if (step === 'ai' && !root()?.contains(document.activeElement)) draw();
        else if (step === 'ai') poller = setTimeout(() => draw(), 2000);
      }, 2000);
    }
  }
  const go = async next => {
    step = next;
    await draw();
  };
  const next = () => go(STEPS[Math.min(STEPS.length - 1, STEPS.findIndex(([id]) => id === step) + 1)][0]);
  onRoute(async route => {
    if (route.view !== 'setup') return;
    step = 'welcome';
    await load();
    await draw();
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (form?.id !== 'setup-form') return;
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    try {
      const changes = stepChanges(step, values);
      if (Object.keys(changes).length) {
        const saved = await requestJSON('/api/settings', { method: 'PUT', body: JSON.stringify({ changes }) });
        data.values = saved.values;
        window.dispatchEvent(new CustomEvent('laika:settings-changed', { detail: saved.values }));
      }
      if (step === 'notify') {
        const targets = Object.fromEntries(['discord_webhook', 'ntfy_url'].filter(key => String(values[key] || '').trim()).map(key => [key, values[key].trim()]));
        if (Object.keys(targets).length) await requestJSON('/api/notifications/targets', { method: 'PUT', body: JSON.stringify(targets) });
      }
      if (step === 'safety') {
        await requestJSON('/api/setup/done', { method: 'POST' });
        // Worker count, schedules and AI choices take effect after a safe restart.
        await requestJSON('/api/settings/apply', { method: 'POST', body: JSON.stringify({ what: 'apply', request_id: newRequestId() }) }).catch(() => null);
      }
      await next();
    } catch (error) {
      const errors = error.body?.detail?.errors;
      say(errors ? Object.values(errors).join('. ') : error.message);
    }
  });
  // Provider cards also sit on Settings → AI: there, redraw that page (and
  // keep redrawing while a sign-in is in progress).
  let settingsPolls = 0;
  async function redraw(inSetup) {
    if (inSetup) return draw();
    window.dispatchEvent(new CustomEvent('laika:settings-redraw'));
    if (settingsPolls) return;
    settingsPolls = 150;
    const tick = async () => {
      settingsPolls -= 1;
      const providers = await requestJSON('/api/ai-providers').catch(() => ({}));
      const busy = Object.values(providers.login || {}).some(login => login && ['starting', 'waiting', 'checking'].includes(login.state));
      const typing = document.activeElement?.name === 'claude_code';
      if (!typing) window.dispatchEvent(new CustomEvent('laika:settings-redraw'));
      if ((busy || typing) && settingsPolls > 0 && location.hash.startsWith('#/settings/ai')) setTimeout(tick, 2000);
      else settingsPolls = 0;
    };
    setTimeout(tick, 2000);
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('button, a');
    if (!button) return;
    const inSetup = Boolean(root()?.contains(button));
    if (!inSetup && !button.closest('.provider-card')) return;
    if (!data.providers) data.providers = {};
    try {
      if (button.dataset.setupBack !== undefined) await go(STEPS[Math.max(0, STEPS.findIndex(([id]) => id === step) - 1)][0]);
      else if (button.dataset.setupSkip !== undefined) {
        if (step === 'safety') await requestJSON('/api/setup/done', { method: 'POST' });
        await next();
      } else if (button.dataset.providerLogin) {
        button.disabled = true;
        await requestJSON(`/api/ai-providers/${encodeURIComponent(button.dataset.providerLogin)}/login`, { method: 'POST', body: JSON.stringify({ request_id: newRequestId() }) });
        data.providers.login = { ...(data.providers.login || {}), [button.dataset.providerLogin]: { state: 'starting' } };
        await redraw(inSetup);
      } else if (button.dataset.claudeCode !== undefined) {
        const input = button.closest('.provider-card').querySelector('input[name="claude_code"]');
        await requestJSON('/api/ai-providers/claude/code', { method: 'POST', body: JSON.stringify({ code: input.value.trim() }) });
        data.providers.login = { ...(data.providers.login || {}), claude: { state: 'checking' } };
        input.blur();
        await redraw(inSetup);
      } else if (button.dataset.providerKey) {
        const input = button.closest('.provider-card').querySelector(`input[name="${button.dataset.providerKey}"]`);
        const saved = await requestJSON('/api/ai-providers/keys', { method: 'PUT', body: JSON.stringify({ [button.dataset.providerKey]: input.value.trim() }) });
        data.providers.keys = saved.keys;
        await requestJSON('/api/ai-providers/refresh', { method: 'POST', body: JSON.stringify({ request_id: newRequestId() }) }).catch(() => null);
        if (inSetup) {
          await draw();
          say('Key saved');
        } else await redraw(false);
      } else if (button.dataset.providerRefresh !== undefined) {
        await requestJSON('/api/ai-providers/refresh', { method: 'POST', body: JSON.stringify({ request_id: newRequestId() }) });
        setTimeout(async () => {
          data.providers = await requestJSON('/api/ai-providers').catch(() => data.providers);
          draw();
        }, 4000);
        say('Checking…');
      } else if (button.dataset.setupTestNotify !== undefined) {
        const status = document.getElementById('setup-notify-status');
        const form = document.getElementById('setup-form');
        const values = Object.fromEntries(new FormData(form).entries());
        const targets = Object.fromEntries(['discord_webhook', 'ntfy_url'].filter(key => String(values[key] || '').trim()).map(key => [key, values[key].trim()]));
        if (Object.keys(targets).length) await requestJSON('/api/notifications/targets', { method: 'PUT', body: JSON.stringify(targets) });
        await requestJSON('/api/notifications/test', { method: 'POST' });
        if (status) status.textContent = 'Sent. Check Discord or ntfy.';
      }
    } catch (error) {
      button.disabled = false;
      if (inSetup) say(error.message);
      else window.alert(error.message);
    }
  });
}
