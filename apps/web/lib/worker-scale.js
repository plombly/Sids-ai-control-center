// Worker scaling (apps/api/scale_routes.py, services/scaler/laika_scaler.py):
// how many workers run and why, with one more / one fewer. Shown on the
// Worker activity panel and on Settings → Workers.
import { requestJSON } from './api.js';
import { esc } from './format.js';
import { clockText } from './elapsed.js';

export function scaleMarkup(data, now = Date.now() / 1000, withLog = false) {
  const state = data?.state;
  if (!state) return '<div class="worker-scale"><span class="subtle">Worker scaler not running: worker count fixed.</span></div>';
  const mode = state.auto ? `Automatic, ${esc(state.min)}–${esc(state.max)}` : 'Fixed number';
  const changing = state.running !== state.target ? ` <span class="pill warn">${state.running > state.target ? 'finishing jobs, then stopping' : 'starting'} → ${esc(state.target)}</span>` : '';
  const waiting = state.runnable ? `${esc(state.runnable)} ready to run${state.projects > 1 ? ` in ${esc(state.projects)} projects` : ''}` : 'nothing waiting';
  const hold = data.hold || state.hold ? `<div class="notice warn-notice"><b>New work paused:</b> ${esc(data.hold || 'memory critical')}. Running jobs continue.</div>` : '';
  const atMax = state.auto && state.target >= state.max;
  const log = withLog && data.log?.length
    ? `<ul class="scale-log">${data.log.slice(0, 8).map(entry => `<li><span class="subtle">${esc(clockText(entry.at, now))}</span> → ${esc(entry.target)}: ${esc(entry.reason)}</li>`).join('')}</ul>`
    : '';
  return `<div class="worker-scale"><div class="scale-row"><span class="scale-count"><b>${esc(state.running)}</b> worker${state.running === 1 ? '' : 's'}${changing}</span><span class="subtle">${mode} · ${waiting} · memory ${esc(Math.round(state.memory_percent))}% free · load ${esc(state.load5)}/${esc(state.cpus)} CPUs</span><span class="scale-buttons"><button type="button" data-scale="-1" aria-label="One fewer worker"${state.target <= 1 ? ' disabled' : ''}>−</button><button type="button" data-scale="1" aria-label="One more worker"${atMax ? ' disabled title="At the most workers (Settings → Workers)"' : ''}>+</button></span></div>${
    state.auto ? '<p class="subtle scale-note">Adds a worker only when jobs that could run side by side wait; removes idle ones. Your + / − holds for 10 minutes.</p>' : ''
  }${hold}${log}</div>`;
}

if (typeof document !== 'undefined') {
  const roots = () => [...document.querySelectorAll('[data-worker-scale]')].filter(node => !node.closest('[hidden]'));
  async function refresh() {
    const nodes = roots();
    if (!nodes.length) return;
    const data = await requestJSON('/api/workers/scale').catch(() => null);
    if (!data) return;
    for (const node of nodes) node.innerHTML = scaleMarkup(data, Date.now() / 1000, node.dataset.workerScale === 'log');
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-scale]');
    if (!button) return;
    button.disabled = true;
    try {
      await requestJSON('/api/workers/scale', { method: 'POST', body: JSON.stringify({ delta: Number(button.dataset.scale) }) });
    } catch (error) {
      window.alert(error.message);
    }
    refresh();
  });
  window.addEventListener('laika:worker-scale-refresh', refresh);
  setInterval(refresh, 10000);
  setTimeout(refresh, 500);
}
