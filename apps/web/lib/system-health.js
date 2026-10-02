import { requestJSON } from './api.js';
import { esc, pill, text } from './format.js';
import { registerPanel } from './registry.js';

const reportEmpty = '<div class="empty">No watchdog report yet (is laika-watchdog.timer running?)</div>';

function backupTime(value) {
  const match = typeof value === 'string' && value.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
  return match ? `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]} UTC` : esc(text(value));
}

function backupMarkup(backup) {
  if (!backup || typeof backup !== 'object') return '<div class="item subtle">No backup recorded yet</div>';

  const errors = backup.errors && typeof backup.errors === 'object' && !Array.isArray(backup.errors)
    ? Object.keys(backup.errors).map(esc).join(', ')
    : '';
  return `<div class="item">Last backup: ${backupTime(backup.at)} ${pill(backup.ok ? 'ok' : 'failed')}${
    !backup.ok && errors ? ` <span class="subtle">${errors}</span>` : ''
  }</div>`;
}

function reportMarkup(report) {
  if (!report || typeof report !== 'object') return reportEmpty;

  const age = typeof report.age_seconds === 'number' && Number.isFinite(report.age_seconds) ? report.age_seconds : null;
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const levels = ['fail', 'warn', 'ok'];
  const ordered = levels.flatMap(level => checks.filter(check => check?.level === level));
  ordered.push(...checks.filter(check => !levels.includes(check?.level)));
  const rows = ordered
    .map(
      check => `<tr><td>${esc(text(check?.name))}</td><td>${pill(check?.level)}</td><td>${esc(text(check?.detail))}</td></tr>`
    )
    .join('');
  const checked = age === null ? '' : ` <span class="subtle">checked ${esc(`${age}`)}s ago</span>`;
  const stale = age !== null && age > 360 ? ` ${pill('Watchdog report is stale')}` : '';

  return `<div class="stack"><div class="item"><div class="item-head">${pill(report.status)}${checked}${stale}</div></div>
    <div class="table-wrap"><table class="job-table"><thead><tr><th>Check</th><th>Level</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

export function healthMarkup(data) {
  const source = data && typeof data === 'object' ? data : {};
  return `${reportMarkup(source.report)}${backupMarkup(source.backup)}`;
}

let lastData = null;
let started = false;

function ensurePanel() {
  if (typeof document === 'undefined') return null;
  const existing = document.getElementById('system-health-panel');
  if (existing) return existing;
  const grid = document.querySelector('.dashboard-grid');
  if (!grid) return null;
  const panel = document.createElement('section');
  panel.className = 'panel wide';
  panel.id = 'system-health-panel';
  panel.innerHTML = '<div class="panel-heading"><div><p class="eyebrow">SYSTEM</p><h2>Health and backups</h2></div></div><div class="stack" data-system-health-body></div>';
  grid.append(panel);
  return panel;
}

function render() {
  if (typeof document === 'undefined') return;
  const panel = ensurePanel();
  const body = panel?.querySelector('[data-system-health-body]');
  if (body && lastData) body.innerHTML = healthMarkup(lastData);
}

async function refresh() {
  try {
    lastData = await requestJSON('/api/system-health');
    render();
  } catch {}
}

function init() {
  if (typeof document === 'undefined' || started) return;
  if (!ensurePanel()) return;
  started = true;
  refresh();
  setInterval(refresh, 30000);
}

if (typeof document !== 'undefined') {
  registerPanel(init);
  init();
}
