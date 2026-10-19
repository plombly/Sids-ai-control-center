// Pause all new work (apps/api/why_routes.py): workers take no new job and
// no new goal is planned; running jobs finish and approved changes merge.
// A banner on every page while paused; the switch is in Settings → Workers.
import { requestJSON } from './api.js';
import { esc } from './format.js';
import { registerClick, registerPanel } from './registry.js';

export function pauseBannerMarkup(state) {
  if (!state?.paused) return '';
  const who = state.by ? ` by ${esc(state.by)}` : '';
  const why = state.reason ? `: ${esc(state.reason)}` : '';
  return `<div class="pause-banner"><b>All new work is paused${who}${why}.</b> Running jobs finish; nothing new starts. <button type="button" data-pause-set="resume">Resume</button></div>`;
}

export function pauseCardMarkup(state) {
  const paused = Boolean(state?.paused);
  return `<h3>Pause all new work</h3><p class="subtle">${paused ? 'Paused: workers take no new job and no new goal is planned. Running jobs finish; approved changes still merge.' : 'Stops workers taking new jobs and goals being planned, for maintenance or to save your plan limits. Running jobs finish; approved changes still merge.'}</p><div class="form-row">${paused ? '<button type="button" data-pause-set="resume">Resume work</button>' : '<input name="pause_reason" maxlength="200" placeholder="Reason (optional)"><button type="button" data-pause-set="pause">Pause all new work</button>'}</div>`;
}

let state = { paused: false };
let fetchedAt = 0;

async function refresh(force = false) {
  if (!force && Date.now() - fetchedAt < 15000) return;
  fetchedAt = Date.now();
  try {
    state = await requestJSON('/api/system/pause');
  } catch {
    return;
  }
  draw();
}

function draw() {
  let banner = document.getElementById('pause-banner');
  const html = pauseBannerMarkup(state);
  if (!banner && html) {
    banner = document.createElement('div');
    banner.id = 'pause-banner';
    document.querySelector('main')?.prepend(banner);
  }
  if (banner) banner.innerHTML = html;
  const card = document.querySelector('[data-pause-card]');
  if (card) card.innerHTML = pauseCardMarkup(state);
}

if (typeof document !== 'undefined') {
  registerPanel(() => refresh());
  window.addEventListener('laika:settings-loaded', () => refresh(true));
  registerClick('pauseSet', async button => {
    const paused = button.dataset.pauseSet === 'pause';
    const reason = document.querySelector('input[name="pause_reason"]')?.value || '';
    button.disabled = true;
    try {
      state = await requestJSON('/api/system/pause', { method: 'POST', body: JSON.stringify({ paused, reason }) });
    } catch (error) {
      window.alert(error.message);
    }
    button.disabled = false;
    draw();
  });
}
