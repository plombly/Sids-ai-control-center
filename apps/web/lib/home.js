// The dashboard's home view: what needs you, what LAIka is doing, your
// projects and what finished recently, plus a box to give LAIka new work.
// Built from the data app.js already polls (renderPanels(state)) plus the
// project list and system health. The detailed panels stay available under
// "Details" (index.html).
import { requestJSON } from './api.js';
import { esc, escValue, text } from './format.js';
import { registerPanel } from './registry.js';
import { assistantMarkup } from './goal-assistant.js';
import { nestProjects } from './project-groups.js';
import { previewMarkup } from './markup.js';
import { elapsedMarkup } from './elapsed.js';

const ACTIVE_GOAL = /^(queued|planning|planned|running|blocked|in_progress|dispatched)$/;
const FINISHED_GOAL = { completed: 'done', failed: 'failed', planning_failed: 'failed' };
const RUNNING_JOB = { claimed: 'starting', running: 'building', testing: 'testing', reviewing: 'reviewing', integrating: 'checking', repairing: 'fixing', awaiting_review: 'in review' };
const ROLE_WORD = { builder: 'Building', reviewer: 'Reviewing', repair: 'Fixing review notes', integrate: 'Checking it fits main' };

export function timeAgo(seconds, now = Date.now() / 1000) {
  const age = Math.max(0, now - (Number(seconds) || 0));
  if (!seconds) return '';
  if (age < 60) return 'just now';
  if (age < 3600) return `${Math.floor(age / 60)} min ago`;
  if (age < 86400) return `${Math.floor(age / 3600)} h ago`;
  return `${Math.floor(age / 86400)} d ago`;
}

const firstLine = value => {
  const line = text(value, '').split('\n').map(item => item.trim()).find(Boolean) || '';
  return line.length > 120 ? `${line.slice(0, 117)}…` : line;
};
const projectChip = (id, names = {}) => `<span class="chip">${esc(names[id] || id || 'laika')}</span>`;

// --- status line -----------------------------------------------------------------------

export function statusMarkup({ health, workers = [], jobs = [], projects = [] }) {
  const report = health?.report || health || {};
  const problems = (report.checks || []).filter(check => check.level !== 'ok');
  const level = report.status || (problems.length ? 'warn' : 'ok');
  const busy = workers.filter(worker => /^(working|busy)$/i.test(text(worker.status, ''))).length;
  const queued = jobs.filter(job => job.status === 'queued').length;
  const headline =
    level === 'ok'
      ? 'Everything is running normally'
      : `${problems.length === 1 ? 'One thing needs' : `${problems.length} things need`} a look: ${problems.map(check => check.name).join(', ')}`;
  return `<div class="home-status level-${escValue(level)}"><span class="status-dot"></span><div><strong>${esc(headline)}</strong><div class="home-facts"><span>${esc(workers.length)} workers · ${esc(busy)} busy</span><span>${esc(queued)} queued</span><span>${esc(projects.length)} projects</span></div></div><button type="button" class="detail-button" data-home-details>${level === 'ok' ? 'Details' : 'See what is wrong'}</button></div>`;
}

// --- needs you ---------------------------------------------------------------------------

// Projects the LAIka builder manages (LAIka itself, its app): nothing to
// approve or answer for them here.
export const viewOnlyIds = (projects = []) => new Set(['laika', ...projects.filter(project => project.view_only).map(project => project.id)]);
const workable = (projects = []) => projects.filter(project => !project.view_only && project.id !== 'laika');

export function needsYou({ approvals = [], jobs = [], dismissed = new Set(), viewOnly = new Set(['laika']) }) {
  const mine = job => !viewOnly.has(job.project_id || 'laika');
  const ready = approvals.filter(job => job.status === 'awaiting_review' && job.review_verdict === 'pass' && mine(job));
  const stuck = jobs.filter(job => job.status === 'needs_human' && !dismissed.has(job.id) && mine(job));
  return { ready, stuck };
}

export function needsYouMarkup(items, names = {}) {
  const { ready, stuck } = items;
  if (!ready.length && !stuck.length) return '<div class="home-empty">Nothing needs you right now.</div>';
  const approval = job => `<article class="home-card attention"><div class="card-top">${projectChip(job.project_id, names)}<span class="card-state ok">Ready to approve</span></div><h3>${esc(job.title || job.id)}</h3><p class="subtle">Tests and review passed. Approving puts it into main.</p><div class="card-actions"><button type="button" class="approve-button" data-op="approve" data-job="${escValue(job.id)}" data-status="${escValue(job.status)}" data-candidate="${escValue(job.integrated_candidate_commit)}">Approve</button>${previewMarkup(job)}<button type="button" class="detail-button" data-detail="${escValue(job.id)}">Details</button><button type="button" class="danger-button" data-op="reject" data-job="${escValue(job.id)}" data-status="${escValue(job.status)}">Reject</button></div></article>`;
  const stuckCard = job => `<article class="home-card attention warn"><div class="card-top">${projectChip(job.project_id, names)}<span class="card-state warn">Stuck</span></div><h3>${esc(job.title || job.id)}</h3><p class="subtle">LAIka gave up after several tries${job.error ? `: ${esc(firstLine(job.error))}` : ''}.</p><div class="card-actions"><button type="button" data-op="extend" data-job="${escValue(job.id)}" data-status="needs_human">Try again</button><button type="button" class="detail-button" data-detail="${escValue(job.id)}">Details</button><button type="button" class="danger-button" data-op="reject" data-job="${escValue(job.id)}" data-status="needs_human">Give up</button></div></article>`;
  // Tests that need the internet ask first (services/network_access.py).
  const networkCard = job => `<article class="home-card attention warn"><div class="card-top">${projectChip(job.project_id, names)}<span class="card-state warn">Wants internet</span></div><h3>${esc(job.title || job.id)}</h3><p class="subtle">Its ${esc(job.network_request_step || 'tests')} seem to need internet access, which tests don't have by default.</p>${job.network_request_reason ? `<pre class="network-reason">${esc(job.network_request_reason)}</pre>` : ''}<div class="card-actions"><button type="button" data-op="network_once" data-job="${escValue(job.id)}" data-status="needs_human">Allow for this change</button><button type="button" data-op="network_always" data-job="${escValue(job.id)}" data-status="needs_human">Always allow in this project</button><button type="button" data-op="network_deny" data-job="${escValue(job.id)}" data-status="needs_human">Keep tests offline</button><button type="button" class="detail-button" data-detail="${escValue(job.id)}">Details</button></div></article>`;
  const card = job => (job.needs_human_kind === 'network' ? networkCard(job) : stuckCard(job));
  return `<div class="home-cards">${ready.map(approval).join('')}${stuck.map(card).join('')}</div>`;
}

// --- in progress -------------------------------------------------------------------------

export function inProgressMarkup({ goals = [], jobs = [] }, names = {}, now = Date.now() / 1000) {
  const active = goals.filter(goal => ACTIVE_GOAL.test(text(goal.status, '')));
  if (!active.length) return '<div class="home-empty">LAIka is idle. Give it something to do above.</div>';
  return `<div class="home-cards">${active
    .map(goal => {
      const progress = goal.progress || {};
      const total = Number(progress.total || goal.total) || 0;
      const done = Number(progress.completed || goal.completed) || 0;
      const percent = total ? Math.round((done / total) * 100) : 0;
      const current = jobs.find(job => job.goal_id === goal.id && RUNNING_JOB[job.status]);
      const step = goal.status === 'queued' ? 'Waiting to be planned' : goal.status === 'planning' ? 'Planning the work' : current ? `${ROLE_WORD[current.role] || 'Working'}: ${firstLine(current.title)}` : total ? 'Waiting for a free worker' : 'Planning the work';
      const elapsed = current && current.started_at != null ? ` · ${elapsedMarkup(current.started_at, current.finished_at, now)}` : '';
      return `<article class="home-card"><div class="card-top">${projectChip(goal.project_id, names)}<span class="subtle">${esc(timeAgo(goal.created_at, now))}</span></div><h3>${esc(firstLine(goal.summary || goal.prompt) || goal.id)}</h3><div class="progress"><i style="width:${percent}%"></i></div><p class="subtle">${total ? `${esc(done)} of ${esc(total)} steps done · ` : ''}${esc(step)}${elapsed}</p></article>`;
    })
    .join('')}</div>`;
}

// --- projects ----------------------------------------------------------------------------

// A parent's children, listed inside its card (each opens its own page).
const membersMarkup = (members = []) =>
  members.length
    ? `<ul class="card-children">${members.map(child => `<li><a class="child-link" href="#/projects/${encodeURIComponent(child.id)}">↳ ${esc(child.name || child.id)}</a></li>`).join('')}</ul>`
    : '';

export function projectsMarkup(projects = [], hostname = globalThis.location?.hostname || 'localhost') {
  if (!projects.length) return '<div class="home-empty">No projects yet. <a href="#/projects/new">Create one</a>.</div>';
  return `<div class="home-projects">${nestProjects(projects)
    .map(project => {
      const counts = project.counts || {};
      const busy = (counts.jobs_running || 0) + (counts.jobs_queued || 0);
      const app = project.app;
      const appLine =
        app?.state === 'running' && app.port
          ? `<a class="app-link" href="http://${escValue(hostname)}:${escValue(app.port)}/" target="_blank" rel="noopener">App running · open</a>`
          : app?.state && app.state !== 'stopped'
            ? `<span class="app-link warn">App ${esc(app.state.replace('_', ' '))}</span>`
            : '';
      // The whole card opens the project (stretched link); the app link sits above it.
      return `<article class="home-project"><div class="card-top"><a class="project-link" href="#/projects/${encodeURIComponent(project.id)}"><strong>${esc(project.name || project.id)}</strong></a><span class="importance imp-${escValue(project.importance)}">${esc(project.importance || '')}</span></div><span class="subtle">${busy ? `${esc(busy)} job${busy === 1 ? '' : 's'} in progress` : 'Idle'}${counts.jobs_awaiting_approval ? ` · ${esc(counts.jobs_awaiting_approval)} to approve` : ''}</span>${appLine}${membersMarkup(project.members)}</article>`;
    })
    .join('')}<article class="home-project add"><a class="project-link" href="#/projects/new"><strong>+ New project</strong></a><span class="subtle">Start empty or import from GitHub</span></article></div>`;
}

// --- recently finished -------------------------------------------------------------------

export function recentMarkup(goals = [], names = {}, now = Date.now() / 1000) {
  const finished = goals
    .filter(goal => FINISHED_GOAL[goal.status])
    .sort((a, b) => (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0))
    .slice(0, 6);
  if (!finished.length) return '<div class="home-empty">Nothing finished yet.</div>';
  return `<ul class="home-recent">${finished
    .map(goal => {
      const ok = goal.status === 'completed';
      return `<li><span class="mark ${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✕'}</span><span class="recent-title">${esc(firstLine(goal.summary || goal.prompt) || goal.id)}</span>${projectChip(goal.project_id, names)}<span class="subtle">${esc(timeAgo(goal.updated_at, now))}</span></li>`;
    })
    .join('')}</ul>`;
}

// --- new work ----------------------------------------------------------------------------

export function projectSelectMarkup(projects = [], chosen = '') {
  const choices = workable(projects);
  const options = choices.length
    ? choices.map(project => `<option value="${escValue(project.id)}"${project.id === chosen ? ' selected' : ''}>${esc(project.name || project.id)}</option>`).join('')
    : '<option value="" disabled selected>Create a project first</option>';
  return `<label class="composer-project">Project <select id="home-goal-project">${options}</select></label>`;
}

// The goal box is the goal assistant (lib/goal-assistant.js) with a project picker.
export function composerMarkup(projects = []) {
  return assistantMarkup('home', {
    label: 'What should LAIka work on?',
    placeholder: 'e.g. Add a contact page with a form that emails me',
    extra: () => projectSelectMarkup(projects.length ? projects : currentProjects(), typeof document === 'undefined' ? '' : document.getElementById('home-goal-project')?.value || '')
  });
}

// --- wiring ------------------------------------------------------------------------------

let projects = [];
const currentProjects = () => projects;
let health = null;
const drawn = {};

function paint(id, html) {
  const node = document.getElementById(id);
  if (node && drawn[id] !== html) {
    node.innerHTML = html;
    drawn[id] = html;
  }
}

function draw(state) {
  if (typeof document === 'undefined' || !document.getElementById('home')) return;
  const get = key => (Array.isArray(state?.[key]?.data) ? state[key].data : []);
  const names = Object.fromEntries(projects.map(project => [project.id, project.id === 'laika' ? 'LAIka' : project.name || project.id]));
  const items = needsYou({ approvals: get('approvals'), jobs: get('jobs'), dismissed: state?.dismissed || new Set(), viewOnly: viewOnlyIds(projects) });
  const count = items.ready.length + items.stuck.length;
  paint('home-status', statusMarkup({ health, workers: get('workers'), jobs: get('jobs'), projects }));
  paint('home-needs-count', count ? String(count) : '');
  paint('home-needs', needsYouMarkup(items, names));
  paint('home-progress', inProgressMarkup({ goals: get('goals'), jobs: get('jobs') }, names));
  paint('home-projects', projectsMarkup(projects));
  paint('home-recent', recentMarkup(get('goals'), names));
}

let lastState = null;

async function refreshSide() {
  try {
    projects = await requestJSON('/api/projects');
  } catch {}
  try {
    health = await requestJSON('/api/system-health');
  } catch {}
  // Only the project options change; whatever is typed in the box stays.
  const select = document.getElementById('home-goal-project');
  const ids = workable(projects).map(project => project.id).join(',');
  if (select && select.dataset.ids !== ids) {
    const chosen = select.value;
    // projectSelectMarkup's <option>s, without its <label>/<select> wrapper.
    select.innerHTML = projectSelectMarkup(projects).replace(/^.*?<select[^>]*>|<\/select>.*$/g, '');
    select.dataset.ids = ids;
    if (chosen && workable(projects).some(project => project.id === chosen)) select.value = chosen;
  }
  draw(lastState);
}

if (typeof document !== 'undefined') {
  registerPanel(state => {
    lastState = state;
    draw(state);
  });
  const start = () => {
    const composer = document.getElementById('home-composer');
    if (composer && !composer.firstChild) composer.innerHTML = composerMarkup(projects);
    refreshSide();
    setInterval(refreshSide, 10000);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  document.addEventListener('click', event => {
    if (!event.target.closest('[data-home-details]')) return;
    const details = document.getElementById('advanced');
    if (details) {
      details.open = true;
      details.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
}
