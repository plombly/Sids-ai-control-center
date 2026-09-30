import { requestJSON } from './api.js';
import { esc, pill, number } from './format.js';
import { registerPanel } from './registry.js';

const emptyMarkup = '<div class="empty">No projects</div>';

const count = value => number(value === null || value === undefined ? 0 : value);

export function overviewMarkup(projects) {
  if (!Array.isArray(projects)) return emptyMarkup;

  const activeProjects = projects.filter(project => project?.status !== 'archived');
  if (!activeProjects.length) return emptyMarkup;

  const rows = activeProjects
    .map(project => {
      const counts = project?.counts && typeof project.counts === 'object' ? project.counts : {};
      const stats = project?.stats && typeof project.stats === 'object' ? project.stats : {};
      const id = encodeURIComponent(project?.id === null || project?.id === undefined ? '' : String(project.id));
      const awaitingApproval = count(counts.jobs_awaiting_approval);
      const needsHuman = count(counts.jobs_needs_human);
      const awaitingCell =
        awaitingApproval === '0' ? awaitingApproval : `${pill('waiting on you')} ${awaitingApproval}`;
      const needsHumanCell = needsHuman === '0' ? needsHuman : `${pill('needs you')} ${needsHuman}`;

      return `<tr><td><a href="#/projects/${esc(id)}">${esc(project?.name)}</a></td><td>${pill(project?.importance)}</td><td>${count(
        counts.jobs_merged
      )} merged / ${number(stats.remaining_effort)} remaining</td><td>${count(counts.jobs_running)}</td><td>${count(
        counts.jobs_queued
      )}</td><td>${awaitingCell}</td><td>${needsHumanCell}</td></tr>`;
    })
    .join('');

  return `<div class="table-wrap"><table class="job-table"><thead><tr><th>Project</th><th>Importance</th><th>Progress</th><th>Running</th><th>Waiting</th><th>Awaiting approval</th><th>Needs human</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

let lastProjects = null;
let hasResult = false;
let started = false;

function ensurePanel() {
  if (typeof document === 'undefined') return null;
  const existing = document.getElementById('project-overview-panel');
  if (existing) return existing;

  const grid = document.querySelector('.dashboard-grid');
  if (!grid) return null;

  const panel = document.createElement('section');
  panel.className = 'panel wide';
  panel.id = 'project-overview-panel';
  panel.innerHTML =
    '<div class="panel-heading"><div><p class="eyebrow">PROJECTS</p><h2>Progress by project</h2></div></div><div class="stack" data-project-overview-body></div>';
  grid.insertBefore(panel, grid.firstChild);
  return panel;
}

function render() {
  if (typeof document === 'undefined') return;
  const panel = ensurePanel();
  const body = panel?.querySelector('[data-project-overview-body]');
  if (body && hasResult) body.innerHTML = overviewMarkup(lastProjects);
}

async function refresh() {
  try {
    lastProjects = await requestJSON('/api/projects');
    hasResult = true;
    render();
  } catch {}
}

function init() {
  if (typeof document === 'undefined' || started) return;
  if (!ensurePanel()) return;
  started = true;
  refresh();
  setInterval(refresh, 10000);
}

if (typeof document !== 'undefined') {
  registerPanel(init);
  init();
}
