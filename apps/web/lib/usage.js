// Usage panel on the dashboard: Claude cost and Codex tokens per project,
// per day and per role (GET /api/usage).
import { requestJSON } from './api.js';
import { esc, escValue, number } from './format.js';
import { registerPanel } from './registry.js';

const RANGES = [7, 30, 90];

export const money = value => `$${(Number(value) || 0).toFixed(2)}`;
export const compact = value => {
  const n = Number(value) || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}k`;
  return String(Math.round(n));
};

export function usageMarkup(data, days = 30) {
  if (!data || !data.total) return '<div class="empty">No usage yet</div>';
  const provider = name => (data.by_provider || []).find(row => row.provider === name) || {};
  const claude = provider('claude');
  const codex = provider('codex');
  const hours = (Number(data.total.seconds) || 0) / 3600;
  const headline = `<div class="usage-headline"><div><span class="usage-big">${esc(money(claude.cost_usd))}</span><span class="subtle">Claude, ${esc(number(claude.jobs || 0))} runs</span></div><div><span class="usage-big">${esc(compact(codex.effective_tokens))}</span><span class="subtle">Codex tokens, ${esc(number(codex.jobs || 0))} runs</span></div><div><span class="usage-big">${esc(number(data.total.jobs))}</span><span class="subtle">jobs · ${esc(hours.toFixed(1))} h of agent time</span></div></div>`;
  const projects = (data.by_project || [])
    .slice()
    .sort((a, b) => b.cost_usd - a.cost_usd || b.effective_tokens - a.effective_tokens)
    .map(row => `<tr><td><a href="#/projects/${encodeURIComponent(row.project)}">${esc(row.project)}</a></td><td>${esc(number(row.jobs))}</td><td>${esc(money(row.cost_usd))}</td><td>${esc(compact(row.effective_tokens))}</td></tr>`)
    .join('');
  const daysRows = data.by_day || [];
  const peak = Math.max(1, ...daysRows.map(row => Number(row.effective_tokens) || 0));
  const bars = daysRows
    .slice(-Math.min(days, 30))
    .map(row => {
      const height = Math.max(2, Math.round(((Number(row.effective_tokens) || 0) / peak) * 60));
      return `<div class="usage-bar" title="${escValue(`${row.day}: ${compact(row.effective_tokens)} tokens · ${money(row.cost_usd)} · ${row.jobs} jobs`)}"><span style="height:${height}px"></span></div>`;
    })
    .join('');
  const roles = (data.by_role || []).map(row => `<span class="pill">${esc(row.role)} ${esc(number(row.jobs))} · ${esc(money(row.cost_usd))}</span>`).join(' ');
  const choices = RANGES.map(value => `<button type="button" class="${value === days ? 'active' : ''}" data-usage-days="${value}">${value} days</button>`).join('');
  return `<div class="file-tabs usage-range">${choices}</div>${headline}<p class="subtle">Claude cost is what the same usage would cost on the API; it runs on your claude.ai plan. Codex runs on your ChatGPT plan and reports tokens.</p><div class="usage-bars" aria-label="Tokens per day">${bars}</div><div class="stack">${roles}</div><div class="table-wrap"><table class="job-table"><thead><tr><th>Project</th><th>Jobs</th><th>Claude cost</th><th>Tokens</th></tr></thead><tbody>${projects}</tbody></table></div>`;
}

let started = false;
let days = 30;

function ensurePanel() {
  let panel = document.getElementById('usage-panel');
  if (panel) return panel;
  const grid = document.querySelector('.dashboard-grid');
  if (!grid) return null;
  panel = document.createElement('section');
  panel.className = 'panel wide';
  panel.id = 'usage-panel';
  panel.innerHTML = '<div class="panel-heading"><div><p class="eyebrow">USAGE</p><h2>Usage and cost</h2></div></div><div data-usage-body><div class="empty">Loading…</div></div>';
  const overview = document.getElementById('project-overview-panel');
  if (overview?.nextSibling) grid.insertBefore(panel, overview.nextSibling);
  else grid.appendChild(panel);
  panel.addEventListener('click', event => {
    const button = event.target.closest('[data-usage-days]');
    if (!button) return;
    days = Number(button.dataset.usageDays) || 30;
    refresh();
  });
  return panel;
}

async function refresh() {
  const body = ensurePanel()?.querySelector('[data-usage-body]');
  if (!body) return;
  try {
    body.innerHTML = usageMarkup(await requestJSON(`/api/usage?days=${days}`), days);
  } catch (error) {
    body.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

function init() {
  if (typeof document === 'undefined' || started || !ensurePanel()) return;
  started = true;
  refresh();
  setInterval(refresh, 60000);
}

if (typeof document !== 'undefined') {
  registerPanel(init);
  init();
}
