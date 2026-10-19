import { requestJSON } from './api.js';
import { esc, pill, text } from './format.js';
import { registerPanel } from './registry.js';
import { setHTML } from './dom.js';

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

// What a failing watchdog check means and what to do about it.
export const HINTS = {
  units: ['A LAIka service is not running, so part of the pipeline is stopped.', 'Run `sudo laika doctor`; it restarts what it can. `systemctl status <unit>` shows why one failed.'],
  redis: ['Redis holds every job and goal; without it nothing moves.', 'Check `docker ps` for laika-redis, then `sudo laika repair`.'],
  heartbeats: ['A worker or the orchestrator stopped reporting: work may be stuck.', 'Settings → Workers shows which; `scripts/laika-restart.sh workers` restarts them safely.'],
  disk: ['Low disk space: builds, logs and backups start failing.', 'Remove old builds and trash, or let `laika-prune` run; check /var/lib/laika and /var/backups.'],
  memory: ['Low memory: the scaler stops new work to protect running jobs.', 'Fewer workers (Settings → Workers) or more memory for the server.'],
  load: ['The server is very busy: jobs and pages are slow.', 'Lower the number of workers, or wait for running jobs to finish.'],
  backup: ['The last nightly backup failed or is old.', 'Its error is shown here; run `sudo systemctl start laika-backup` to retry, then check this page again.'],
  restore_check: ['The monthly test restore of a backup failed or is overdue.', 'Run `sudo systemctl start laika-restore-check` and read which part failed here afterwards.'],
  pipeline: ['Some jobs need you, or an AI provider is paused.', 'See Needs you on the home page; Settings → AI shows provider pauses and when they lift.'],
  exposure: ['LAIka may be reachable from outside your network.', 'Keep it on your LAN or VPN only: close the port forward or firewall rule it names.'],
  live_tree: ["LAIka's own checkout has changes, so its updates and merges are refused.", 'Run `git status` in LAIka\'s folder and commit or remove the changes.']
};

export const hintMarkup = check => {
  const hint = check && check.level !== 'ok' ? HINTS[check.name] : null;
  return hint ? `<div class="subtle">${esc(hint[0])} <b>What to do:</b> ${esc(hint[1])}</div>` : '';
};

function reportMarkup(report) {
  if (!report || typeof report !== 'object') return reportEmpty;

  const age = typeof report.age_seconds === 'number' && Number.isFinite(report.age_seconds) ? report.age_seconds : null;
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const levels = ['fail', 'warn', 'ok'];
  const ordered = levels.flatMap(level => checks.filter(check => check?.level === level));
  ordered.push(...checks.filter(check => !levels.includes(check?.level)));
  const rows = ordered
    .map(
      check => `<tr><td>${esc(text(check?.name))}</td><td>${pill(check?.level)}</td><td>${esc(text(check?.detail))}${hintMarkup(check)}</td></tr>`
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
  if (body && lastData) setHTML(body, healthMarkup(lastData));
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
