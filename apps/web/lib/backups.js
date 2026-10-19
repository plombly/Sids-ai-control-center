// Settings → System, Backups (apps/api/backups_routes.py): the snapshots on
// this server, the last backup and restore check, and "run now" buttons.
import { requestJSON, newRequestId } from './api.js';
import { esc } from './format.js';
import { registerClick } from './registry.js';

const size = bytes => (bytes > 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`);
const stamp = name => {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/.exec(name || '');
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : esc(name);
};

export function backupsMarkup(data = {}) {
  const snapshots = Array.isArray(data.snapshots) ? data.snapshots : [];
  const rows = snapshots.slice(0, 14)
    .map(s => {
      const problems = Object.keys(s.errors || {});
      return `<tr><td>${stamp(s.name)}</td><td>${esc(size(s.bytes || 0))}</td><td>${esc((s.parts || []).join(', '))}</td><td>${problems.length ? `<span class="pill warn">${esc(problems.join(', '))} failed</span>` : '<span class="pill ok">complete</span>'}</td></tr>`;
    })
    .join('');
  const check = data.restore_check;
  const checkLine = check ? `<p class="subtle">Last restore check: ${check.ok ? 'restored fine' : `<b>failed</b> (${esc((check.checks || []).filter(c => !c.ok).map(c => c.name).join(', '))})`}${check.snapshot ? ` · snapshot ${esc(check.snapshot)}` : ''}</p>` : '<p class="subtle">No restore check has run yet.</p>';
  return `<h3>Backups</h3><p class="subtle">Snapshots of everything LAIka needs (code, Redis, database, settings, projects), kept on this server. A restore check proves the newest one can be restored, in throwaway places.</p>${rows ? `<div class="table-wrap"><table class="job-table"><thead><tr><th>Taken</th><th>Size</th><th>Contains</th><th>State</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="subtle">No snapshots yet (or the first one after this update has not run).</p>'}${checkLine}<div class="form-row"><button type="button" data-backup-run="backup">Back up now</button><button type="button" data-backup-run="restore_check">Check a restore now</button><span class="form-status" data-backup-status role="status"></span></div><p class="field-hint">Restoring is deliberate and done on the server: docs/operations.md (“Backups”) explains each part.</p>`;
}

export async function loadBackups(node) {
  try {
    node.innerHTML = backupsMarkup(await requestJSON('/api/backups'));
  } catch (error) {
    node.innerHTML = `<p class="subtle">${esc(error.message)}</p>`;
  }
}

if (typeof document !== 'undefined') {
  window.addEventListener('laika:settings-loaded', () => {
    const node = document.querySelector('[data-backups-card]');
    if (node) loadBackups(node);
  });
  registerClick('backupRun', async button => {
    const status = document.querySelector('[data-backup-status]');
    button.disabled = true;
    try {
      await requestJSON('/api/backups/run', { method: 'POST', body: JSON.stringify({ what: button.dataset.backupRun, request_id: newRequestId().slice(0, 40) }) });
      if (status) status.textContent = button.dataset.backupRun === 'backup' ? 'Backup started: it takes a few minutes; this list updates when it finishes.' : 'Restore check started: the result appears here in a few minutes.';
    } catch (error) {
      if (status) status.textContent = error.message;
    }
    button.disabled = false;
  });
}
