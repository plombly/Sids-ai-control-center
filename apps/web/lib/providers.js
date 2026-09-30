import { requestJSON } from './api.js';
import { esc, pill, text } from './format.js';
import { registerPanel } from './registry.js';

export function providersMarkup(data) {
  const source = data && typeof data === 'object' ? data : {};
  const claude = source.claude && typeof source.claude === 'object' ? source.claude : {};
  const inUse = Array.isArray(claude.in_use) ? claude.in_use : [];
  const routing = typeof source.routing === 'string' ? source.routing : '';
  const entries = routing
    .split(' · ')
    .map(entry => entry.trim())
    .filter(Boolean)
    .map(entry => {
      const separator = entry.indexOf(' ');
      return separator < 0 ? [entry, ''] : [entry.slice(0, separator), entry.slice(separator + 1).trim()];
    });
  const routingMarkup = entries.length
    ? `<div class="table-wrap"><table class="job-table"><thead><tr><th>Role</th><th>Provider/model</th></tr></thead><tbody>${entries
        .map(([role, model]) => `<tr><td>${esc(role)}</td><td>${esc(model)}</td></tr>`)
        .join('')}</tbody></table></div>`
    : '<div class="empty">Routing unknown</div>';
  const holders = inUse
    .map(item => (item && typeof item === 'object' ? esc(item.holder) : esc(undefined)))
    .join(', ');
  const capacity = `<div class="item">Claude slots: ${esc(inUse.length)}/${claude.limit == null ? '—' : esc(claude.limit)}${
    inUse.length ? ` <span class="subtle">${holders}</span>` : ''
  }</div>`;
  const cooldown = claude.cooling_down
    ? `<div class="item"><div class="item-head">${pill('cooling down')}<span class="item-title">${esc(
        text(claude.cooldown_reason, 'Claude unavailable')
      )}</span></div>${
        typeof claude.cooldown_seconds_left === 'number'
          ? `<div class="subtle">about ${esc(Math.ceil(claude.cooldown_seconds_left / 60))} min left</div>`
          : ''
      }</div>`
    : '';
  return `${routingMarkup}${capacity}${cooldown}`;
}

let lastData = null;

function ensurePanel() {
  const existing = document.getElementById('providers-panel');
  if (existing) return existing;
  const grid = document.querySelector('.dashboard-grid');
  if (!grid) return null;
  const panel = document.createElement('section');
  panel.className = 'panel wide';
  panel.id = 'providers-panel';
  panel.innerHTML =
    '<div class="panel-heading"><div><p class="eyebrow">MODELS</p><h2>Routing and capacity</h2></div></div>' +
    '<div class="stack" data-providers-body></div>';
  grid.append(panel);
  return panel;
}

function render() {
  const panel = ensurePanel();
  const body = panel?.querySelector('[data-providers-body]');
  if (body) body.innerHTML = providersMarkup(lastData);
}

async function refresh() {
  try {
    lastData = await requestJSON('/api/providers');
  } catch (error) {
    if (lastData === null) {
      const banner = document.getElementById('banner');
      if (banner) {
        banner.hidden = false;
        banner.textContent = error instanceof Error ? error.message : 'Unable to load providers';
      }
    }
  }
  render();
}

registerPanel(() => {
  if (typeof document === 'undefined') return;
  ensurePanel();
  if (lastData) render();
});

if (typeof document !== 'undefined') {
  refresh();
  setInterval(refresh, 10000);
}
