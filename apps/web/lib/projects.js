import { requestJSON, operatorRequest, newRequestId } from './api.js';
import { esc, escValue, pill, text, number } from './format.js';
import { registerPanel, registerClick, onRoute } from './registry.js';
import { projectsMarkup as homeProjectsMarkup } from './home.js';

const requestId = newRequestId;

export function projectListMarkup(projects) {
  if (!Array.isArray(projects) || !projects.length) return '<div class="empty">No projects yet</div>';
  return projects.map(project => {
    const id = text(project.id, '');
    const counts = project.counts || {};
    const stats = project.stats || {};
    const countsLine = `queued ${number(counts.jobs_queued)} · running ${number(counts.jobs_running)} · awaiting approval ${number(
      counts.jobs_awaiting_approval
    )} · needs human ${number(counts.jobs_needs_human)} · merged ${number(counts.jobs_merged)}`;
    return `<div class="item"><div class="item-head"><div class="item-title"><a href="#/projects/${encodeURIComponent(
      id
    )}">${esc(project.name)}</a> <span class="subtle">${esc(id)}</span>${id === 'sid' ? ' <span class="pill">this system</span>' : ''}</div><div>${pill(project.importance)}${
      project.status !== 'active' ? ` ${pill(project.status)}` : ''
    }</div></div><div class="subtle">${esc(countsLine)}</div><div class="subtle">remaining effort ${esc(
      number(stats.remaining_effort)
    )}</div></div>`;
  }).join('');
}

export function newProjectFormMarkup() {
  return `<form id="new-project-form" class="goal-form"><div class="form-row"><label>ID <input name="id" required></label><label>Name <input name="name" required></label><label>Importance <select name="importance"><option value="high">high</option><option value="medium" selected>medium</option><option value="low">low</option></select></label></div><div class="form-row"><label>Source <select name="source"><option value="">empty</option><option value="clone">clone</option></select></label><label>URL <input name="url"></label><label>Push remote <input name="push_remote"></label><label>Gate <input name="gate"></label><button type="submit">Create project</button></div><span id="new-project-status" class="form-status" role="status"></span></form>`;
}

// SID's own page: what it is and what is configured on the host instead.
export function systemInfoMarkup(system) {
  const info = system || {};
  const row = (label, value) => `<div><span class="subtle">${esc(label)}</span> ${value}</div>`;
  const head = info.head ? `${esc(info.branch || 'main')} @ ${esc(info.head)} <span class="subtle">${esc(info.subject || '')}</span>` : '<span class="subtle">not reported yet</span>';
  return `<div class="system-info"><h3>This is SID itself</h3><p class="subtle">Goals you submit here improve the command center through the same plan, build, review and approval flow as any project. Its code, tests and GitHub connection are set up on the server, so this page has no build, file, GitHub or delete settings.</p><div class="stack">${row(
    'GitHub',
    info.remote ? esc(info.remote) : '<span class="subtle">not reported yet</span>'
  )}${row('Code', head)}${row('Checks before merge', esc(info.gate || 'scripts/integration-check.py'))}${row(
    'Health',
    '<a href="#/">system health on the dashboard</a>'
  )}</div></div>`;
}

const TAB_LABELS = { overview: 'Overview', activity: 'Activity', files: 'Files', history: 'History', settings: 'Settings' };
const GOAL_DONE = /^(completed|failed|planning_failed|cancelled)$/;

export function tabsMarkup(id, tab) {
  const tabs = id === 'sid' ? ['overview', 'activity', 'history'] : ['overview', 'activity', 'files', 'history', 'settings'];
  return `<nav class="project-tabs" aria-label="Project sections">${tabs
    .map(key => `<a href="#/projects/${encodeURIComponent(id)}${key === 'overview' ? '' : `/${key}`}" class="${key === tab ? 'active' : ''}"${key === tab ? ' aria-current="page"' : ''}>${TAB_LABELS[key]}</a>`)
    .join('')}</nav>`;
}

const firstLine = value => {
  const line = text(value, '').split('\n').map(item => item.trim()).find(Boolean) || '';
  return line.length > 140 ? `${line.slice(0, 137)}…` : line;
};

export function usageLineMarkup(usage) {
  const total = usage?.total;
  if (!total) return '';
  const claude = (usage.by_provider || []).find(row => row.provider === 'claude') || {};
  const codex = (usage.by_provider || []).find(row => row.provider === 'codex') || {};
  const tokens = Number(codex.effective_tokens) || 0;
  return `<div class="usage-line"><span><b>${esc(number(total.jobs))}</b> jobs in 30 days</span><span><b>$${esc((Number(claude.cost_usd) || 0).toFixed(2))}</b> Claude</span><span><b>${esc(tokens >= 1e6 ? `${(tokens / 1e6).toFixed(1)}M` : tokens >= 1e3 ? `${Math.round(tokens / 1e3)}k` : tokens)}</b> Codex tokens</span></div>`;
}

function overviewMarkup(project) {
  const id = text(project?.id, '');
  const name = text(project?.name, id);
  const goals = Array.isArray(project?.goals) ? project.goals : [];
  const jobs = Array.isArray(project?.jobs) ? project.jobs : [];
  const goalItems = goals
    .map(goal => {
      const progress = goal.progress || {};
      const total = Number(progress.total) || 0;
      const done = Number(progress.completed) || 0;
      const percent = total ? Math.round((done / total) * 100) : GOAL_DONE.test(text(goal.status, '')) ? 100 : 0;
      return `<div class="item"><div class="item-head"><span class="item-title">${esc(firstLine(goal.summary ?? goal.prompt))}</span>${pill(goal.status)}</div><div class="progress"><i style="width:${percent}%"></i></div><div class="subtle">${esc(number(done))}/${esc(number(total))} jobs</div></div>`;
    })
    .join('');
  const jobRows = jobs
    .map(job => `<tr><td><button type="button" class="detail-button" data-detail="${esc(job.id)}">${esc(job.id)}</button></td><td>${esc(firstLine(job.title) || '')}</td><td>${pill(job.status)}</td><td class="subtle">${esc(job.provider)}/${esc(job.model)}</td></tr>`)
    .join('');
  return `<form id="project-goal-form" class="goal-form project-composer"><label class="composer-label" for="project-goal-text">What should SID do in ${esc(name)}?</label><textarea id="project-goal-text" name="goal" placeholder="Describe the change you want" required></textarea><div class="composer-row"><label class="composer-atomic"><input type="checkbox" name="atomic"> Small change (one step)</label><button type="submit" class="primary">Start</button></div><span id="project-goal-status" class="form-status" role="status"></span></form>${usageLineMarkup(project.usage)}${id === 'sid' ? systemInfoMarkup(project.system) : appStatusMarkup(project)}<div class="stack"><h3>Goals</h3>${goalItems || '<div class="empty">No goals yet</div>'}</div><h3>Recent jobs</h3><div class="table-wrap"><table class="job-table"><thead><tr><th>Job</th><th>What</th><th>Status</th><th>Agent</th></tr></thead><tbody>${jobRows || '<tr><td colspan="4" class="subtle">No jobs yet</td></tr>'}</tbody></table></div>`;
}

// Activity: one timeline of what happened in the project.
const ACTIVITY = {
  goal_started: ['•', 'info'], goal_done: ['✓', 'ok'], goal_failed: ['✕', 'bad'], merged: ['✓', 'ok'],
  rejected: ['✕', 'muted'], stuck: ['!', 'warn'], undo: ['↶', 'warn'], code_change: ['✎', 'info'],
  deployed: ['▲', 'ok'], app_problem: ['!', 'bad'], preview: ['◐', 'info'], restored: ['↺', 'info']
};

export function activityMarkup(events, now = Date.now() / 1000) {
  const list = Array.isArray(events) ? events : [];
  if (!list.length) return '<div class="empty">Nothing has happened here yet.</div>';
  const ago = seconds => {
    const age = Math.max(0, now - (Number(seconds) || 0));
    if (age < 3600) return `${Math.max(1, Math.floor(age / 60))} min ago`;
    if (age < 86400) return `${Math.floor(age / 3600)} h ago`;
    return new Date((Number(seconds) || 0) * 1000).toLocaleDateString();
  };
  return `<ol class="activity-list">${list
    .map(event => {
      const [icon, tone] = ACTIVITY[event.kind] || ['•', 'info'];
      const isJob = ['merged', 'rejected', 'stuck'].includes(event.kind) && event.ref;
      const ref = isJob ? ` <button type="button" class="detail-button" data-detail="${escValue(event.ref)}">details</button>` : '';
      return `<li class="activity-item"><span class="activity-icon tone-${tone}">${icon}</span><div class="activity-main"><span>${esc(event.title)}${ref}</span>${event.detail ? `<span class="subtle">${esc(event.detail)}</span>` : ''}</div><span class="subtle activity-time">${esc(ago(event.at))}</span></li>`;
    })
    .join('')}</ol>`;
}

// History: main's recent changes; a SID job's commits are one change.
export function historyMarkup(history, now = Date.now() / 1000) {
  const changes = Array.isArray(history?.changes) ? history.changes : [];
  if (!changes.length) return '<div class="empty">No history yet (it appears within a few seconds of the first commit).</div>';
  const ago = seconds => {
    const age = Math.max(0, now - (Number(seconds) || 0));
    return age < 3600 ? `${Math.max(1, Math.floor(age / 60))} min ago` : age < 86400 ? `${Math.floor(age / 3600)} h ago` : `${Math.floor(age / 86400)} d ago`;
  };
  const items = changes
    .map(change => {
      const commits = change.commits || [];
      const undo = history.can_undo
        ? change.kind === 'job'
          ? change.complete === false
            ? ''
            : `<button type="button" class="undo-button" data-undo-job="${escValue(change.job_id)}" data-undo-title="${escValue(change.title)}">Undo</button>`
          : change.root
            ? '' // the project's first commit: nothing to undo it to
            : `<button type="button" class="undo-button" data-undo-commit="${escValue(change.sha)}" data-undo-title="${escValue(change.title)}">Undo</button>`
        : '';
      const detail =
        change.kind === 'job'
          ? `<button type="button" class="detail-button" data-detail="${escValue(change.job_id)}">job ${esc(change.job_id)}</button> · ${esc(commits.length)} commit${commits.length === 1 ? '' : 's'}`
          : `<code>${esc(String(change.sha || '').slice(0, 8))}</code> · ${esc(change.author || '')}`;
      return `<li class="history-item"><div class="history-main"><span class="history-title">${esc(change.title)}</span><span class="subtle">${detail} · ${esc(ago(change.time))}</span></div>${undo}</li>`;
    })
    .join('');
  const note = history.can_undo
    ? 'Undo adds a new commit that reverses the change, so nothing is lost and it can itself be undone.'
    : "SID's own history is shown for reference; its changes go through review instead of Undo.";
  return `<p class="subtle">${note}</p><ul class="history-list">${items}</ul>`;
}

function settingsMarkup(project) {
  const id = text(project?.id, '');
  if (id === 'sid') return systemInfoMarkup(project.system);
  return `${buildSettingsMarkup(project)}${envMarkup(id, project.env)}<form id="project-push-form" class="goal-form push-settings"><h3>GitHub</h3><p class="subtle">Push merged work to a GitHub repository. SID creates a deploy key and shows it here to add to the repository.</p><div class="form-row"><input name="url" placeholder="git@github.com:you/repo.git" required><button type="submit">Set up GitHub push</button></div><span id="project-push-status" class="form-status" role="status"></span></form>${deleteProjectMarkup(id)}`;
}

export function projectDetailMarkup(project, tab = 'overview') {
  const id = text(project?.id, '');
  const name = text(project?.name, id);
  const retry = project?.status === 'pending_key'
    ? `<div class="notice"><button type="button" data-retry-clone="${esc(id)}">Retry import</button><span class="subtle">Add the deploy key shown when the project was created, then retry</span></div>`
    : '';
  const app = project?.app;
  const appLink = app?.state === 'running' && app.port
    ? `<a class="button" href="http://${escValue(globalThis.location?.hostname || 'localhost')}:${escValue(app.port)}/" target="_blank" rel="noopener">Open app</a>`
    : '';
  const body =
    tab === 'history' ? historyMarkup(project.history) : tab === 'settings' ? settingsMarkup(project) : tab === 'activity' ? activityMarkup(project.activity?.events) : tab === 'files' ? '' : overviewMarkup(project);
  return `<section class="panel wide project-page"><div class="project-head"><div><p class="eyebrow">${id === 'sid' ? 'SID · THIS SYSTEM' : 'PROJECT'}</p><h2>${esc(name)}</h2></div><div class="project-head-actions">${appLink}${project.status && project.status !== 'active' ? pill(project.status) : ''}<label class="importance-select">Importance <select id="project-importance" data-project="${esc(id)}"><option value="high"${
    project.importance === 'high' ? ' selected' : ''
  }>high</option><option value="medium"${project.importance === 'medium' ? ' selected' : ''}>medium</option><option value="low"${
    project.importance === 'low' ? ' selected' : ''
  }>low</option></select></label></div></div>${retry}${tabsMarkup(id, tab)}<div class="project-tab-body">${body}</div></section>`;
}

const APP_STATES = {
  running: 'Running', deploying: 'Deploying the latest main…', setup_failed: 'Dependency setup failed',
  crashed: 'Crashed', stopped: 'Stopped', error: 'Error'
};

// The running app: state, link from this browser, restart, last log lines.
export function appStatusMarkup(project, hostname = globalThis.location?.hostname || 'localhost') {
  const id = text(project?.id, '');
  if (!id || id === 'sid' || !text(project?.run_command, '')) return '';
  const app = project.app || {};
  const port = app.port || project.run_port;
  const state = text(app.state, 'starting');
  const url = port ? `http://${hostname}:${port}/` : '';
  const link = state === 'running' && url ? `<a class="button" href="${escValue(url)}" target="_blank" rel="noopener">Open ${esc(url)}</a>` : '';
  const commit = app.commit ? `<span class="subtle">main ${esc(String(app.commit).slice(0, 8))}</span>` : '';
  const error = app.error ? `<div class="form-status">${esc(app.error)}</div>` : '';
  const log = app.log ? `<pre class="app-log">${esc(app.log)}</pre>` : '';
  return `<div class="app-status"><div class="item-head"><span class="item-title">App ${pill(state)} ${esc(APP_STATES[state] || '')}</span>${commit}</div><div class="form-row">${link}<button type="button" data-app-restart="${esc(id)}">Restart</button></div>${error}${log}</div>`;
}

// Dependency setup, tests and the run command; empty means automatic/off.
export function buildSettingsMarkup(project) {
  const id = text(project?.id, '');
  if (!id || id === 'sid') return '';
  const input = (name, label, placeholder, hint) =>
    `<label class="field">${esc(label)}<input name="${name}" value="${escValue(project[name])}" placeholder="${escValue(placeholder)}" autocomplete="off"></label><span class="field-hint">${esc(hint)}</span>`;
  return `<form id="project-settings-form" class="goal-form build-settings"><h3>Build &amp; run</h3>${input(
    'setup_command', 'Install dependencies', 'Detect automatically', 'Runs with internet access before builds and tests (npm ci, pip install …). Tests themselves run offline.'
  )}${input('gate_command', 'Test command', 'Detect automatically', 'Must pass before anything is merged (npm test, pytest …).')}${input(
    'run_command', 'Run command', 'Not running', 'Keeps the app running from the latest main, e.g. npm start. Listen on the PORT environment variable and 0.0.0.0.'
  )}${input('run_port', 'Port', 'Assigned automatically (8100-8199)', 'Open it from your PC at this server\'s address and this port.')}<div class="form-row limits-row">${input(
    'run_memory_mb', 'Memory limit (MB)', '1024', 'Killed and restarted if it uses more.'
  )}${input('run_cpus', 'CPU limit (cores)', '1', 'e.g. 0.5 or 2.')}${input('run_tasks', 'Process limit', '512', 'Threads and processes.')}</div><div class="form-row"><button type="submit">Save settings</button><span id="project-settings-status" class="form-status" role="status"></span></div></form>${appStatusMarkup(project)}`;
}

export function buildSettingsRequest(values) {
  const body = {};
  for (const key of ['setup_command', 'gate_command', 'run_command']) body[key] = text(values[key], '').trim();
  const port = text(values.run_port, '').trim();
  if (port) body.run_port = Number(port);
  for (const key of ['run_memory_mb', 'run_cpus', 'run_tasks']) {
    const value = text(values[key], '').trim();
    if (value) body[key] = Number(value);
  }
  return body;
}

// Deleting wipes the project from the server; SID itself cannot be deleted.
// Secrets for the running app: names only, values are write-only.
export function envMarkup(id, env) {
  if (!id || id === 'sid') return '';
  const variables = Array.isArray(env?.variables) ? env.variables : [];
  const rows = variables.length
    ? variables
        .map(item => `<div class="env-row"><code>${esc(item.name)}</code><span class="subtle">${'•'.repeat(Math.min(8, Math.max(4, item.length || 0)))} (${esc(item.length)} characters)</span><button type="button" class="danger-button" data-env-delete="${escValue(item.name)}">Delete</button></div>`)
        .join('')
    : '<div class="empty">No variables yet</div>';
  return `<form id="project-env-form" class="goal-form env-settings" autocomplete="off"><h3>Environment &amp; secrets</h3><p class="subtle">Given only to the running app (e.g. <code>process.env.API_KEY</code>), never to the AI builders, tests or logs. Values can be replaced but not read back. Saving restarts the app.</p><div class="stack">${rows}</div><div class="form-row"><input name="name" placeholder="NAME" aria-label="Variable name" autocomplete="off" spellcheck="false"><input name="value" type="password" placeholder="value" aria-label="Variable value" autocomplete="new-password"><button type="submit">Save variable</button></div><span id="project-env-status" class="form-status" role="status"></span></form>`;
}

// Deleted projects still restorable (GET /api/projects-trash).
export function trashMarkup(items, now = Date.now() / 1000) {
  if (!Array.isArray(items) || !items.length) return '';
  const hours = seconds => Math.max(0, Math.round(seconds / 3600));
  const rows = items
    .map(item => {
      const left = hours((item.expires_at || 0) - now);
      return `<div class="item"><div class="item-head"><div class="item-title">${esc(item.name)} <span class="subtle">${esc(item.project_id)}</span></div><button type="button" data-restore-trash="${escValue(
        item.trash_id
      )}">Restore</button></div><div class="subtle">Deleted ${esc(new Date((item.deleted_at || 0) * 1000).toLocaleString())} · removed for good in ${left <= 1 ? 'under an hour' : `${left} hours`}</div></div>`;
    })
    .join('');
  return `<section class="panel wide trash-panel"><div class="panel-heading"><div><p class="eyebrow">TRASH</p><h2>Recently deleted</h2></div></div><div class="stack">${rows}</div></section>`;
}

export function deleteProjectMarkup(id) {
  if (!id || id === 'sid') return '';
  return `<form id="project-delete-form" class="goal-form danger-zone"><h3>Delete project</h3><p class="subtle">Moves the project to the trash with its repository, app data, deploy key, goals and jobs, and stops its app. You can restore it from the Projects page for 1 day; after that it is removed for good. Work that is running must finish first.</p><div class="form-row"><input name="confirm" autocomplete="off" placeholder="Type ${esc(
    id
  )} to confirm" aria-label="Project ID to confirm"><button type="submit" class="danger-button">Delete project</button></div><span id="project-delete-status" class="form-status" role="status"></span></form>`;
}

export function publicKeyMarkup(output) {
  let parsed;
  try {
    parsed = typeof output === 'string' ? JSON.parse(output) : null;
  } catch {
    return '';
  }
  return parsed?.public_key
    ? `<pre>${esc(parsed.public_key)}</pre><div class="subtle">Add this key to the GitHub repository as a deploy key with write access</div>`
    : '';
}

export function buildProjectRequest(values) {
  const source = text(values.source, '').trim();
  const body = { id: text(values.id, '').trim(), name: text(values.name, '').trim(), importance: text(values.importance, '').trim(), source };
  const url = text(values.url, '').trim();
  if (source === 'clone') body.url = url;
  for (const key of ['push_remote', 'gate']) {
    const item = text(values[key], '').trim();
    if (item) body[key] = item;
  }
  body.request_id = requestId();
  return body;
}

export function validateProjectValues(values) {
  const id = text(values.id, '').trim();
  const name = text(values.name, '').trim();
  if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(id)) return 'ID must use lowercase letters, numbers, and hyphens (up to 40 characters)';
  if (name.length < 1 || name.length > 80) return 'Name must be 1-80 characters';
  if (text(values.source, '').trim() === 'clone' && !text(values.url, '').trim()) return 'Clone projects require a URL';
  return '';
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const resultMessage = result => text(result?.message || result?.detail || result?.status, 'Submitted');
async function poll(request_id) {
  let result = await operatorRequest(request_id);
  for (let attempt = 0; attempt < 60 && /^(pending|running)$/i.test(text(result?.status, '')); attempt += 1) {
    await sleep(2000);
    result = await operatorRequest(request_id);
  }
  return result;
}

if (typeof document !== 'undefined') {
  let activeRoute = null;
  let refreshTimer = null;
  let renderVersion = 0;
  let busy = false; // an operator request is in flight: keep its status visible
  const root = () => document.getElementById('projects-root');
  // A project page has two parts: the details this module re-renders every
  // few seconds, and the file browser (lib/project-files.js), which keeps
  // its own state and must not be redrawn with them.
  const detailMain = () => {
    const container = root();
    if (!container) return null;
    let main = document.getElementById('project-detail-main');
    if (!main || !container.contains(main)) {
      container.innerHTML = '<div id="project-detail-main"></div><div id="project-files-panel"></div>';
      main = document.getElementById('project-detail-main');
    }
    return main;
  };
  const focusedForm = container => {
    const active = document.activeElement;
    return active && container?.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName);
  };
  const status = (id, message, output) => {
    const node = document.getElementById(id);
    if (node) node.innerHTML = `${esc(message)}${publicKeyMarkup(output)}`;
  };
  async function render(route, force = false) {
    const container = root();
    if (!container || route.view !== 'projects' || route.create || busy || (!force && focusedForm(document.getElementById('project-detail-main') || container))) return;
    const version = ++renderVersion;
    if (route.projectId === null) {
      try {
        const [projects, trashed] = await Promise.all([
          requestJSON('/api/projects?limit=25'),
          requestJSON('/api/projects-trash').catch(() => [])
        ]);
        if (version === renderVersion) container.innerHTML = `<div class="projects-page"><div class="page-head"><h2>Projects</h2><a class="button primary" href="#/projects/new">Create a project</a></div>${homeProjectsMarkup(projects)}${trashMarkup(trashed)}</div>`;
      } catch (error) {
        container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
      }
      return;
    }
    try {
      const id = encodeURIComponent(route.projectId);
      const tab = route.tab || 'overview';
      const optional = path => requestJSON(path).catch(() => null);
      const [project, extra] = await Promise.all([
        requestJSON(`/api/projects/${id}?limit=25`),
        tab === 'settings' && route.projectId !== 'sid' ? optional(`/api/projects/${id}/env`)
          : tab === 'history' ? optional(`/api/projects/${id}/history`)
          : tab === 'activity' ? optional(`/api/projects/${id}/activity`)
          : tab === 'overview' ? optional(`/api/usage?days=30&project=${id}`)
          : null
      ]);
      if (project) {
        if (tab === 'settings') project.env = extra;
        if (tab === 'history') project.history = extra;
        if (tab === 'activity') project.activity = extra;
        if (tab === 'overview') project.usage = extra;
      }
      if (version === renderVersion) {
        detailMain().innerHTML = projectDetailMarkup(project, tab);
        const files = document.getElementById('project-files-panel');
        if (files) files.hidden = tab !== 'files';
      }
    } catch (error) {
      if (version !== renderVersion) return;
      detailMain().innerHTML = `<div class="empty">${error.message === 'HTTP 404' ? 'Project not found' : esc(error.message)}</div>`;
    }
  }
  const formValues = form => Object.fromEntries(new FormData(form).entries());
  onRoute(route => {
    activeRoute = route;
    // A page still loading for the previous route must not draw over this one
    // (e.g. the create wizard): every render checks this version first.
    renderVersion += 1;
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
    if (route.view !== 'projects' || route.create) return;
    if (route.projectId) {
      detailMain(); // before the file browser looks for its slot
      const files = document.getElementById('project-files-panel');
      if (files) files.hidden = route.tab !== 'files';
    }
    render(route, true);
    refreshTimer = setInterval(() => render(activeRoute), 5000);
  });
  registerPanel(() => {
    if (activeRoute?.view === 'projects' && !refreshTimer) render(activeRoute);
  });
  const undo = async button => {
    const title = button.dataset.undoTitle || 'this change';
    if (!globalThis.confirm?.(`Undo "${title}"? SID adds a new commit to main that reverses it.`)) return;
    button.disabled = true;
    button.textContent = 'Undoing…';
    try {
      const request_id = requestId();
      const body = button.dataset.undoJob ? { job: button.dataset.undoJob, request_id } : { commit: button.dataset.undoCommit, request_id };
      const response = await requestJSON(`/api/projects/${encodeURIComponent(activeRoute.projectId)}/undo`, { method: 'POST', body: JSON.stringify(body) });
      const result = await poll(response.request_id || request_id);
      if (result.status !== 'succeeded') throw new Error(resultMessage(result));
      button.textContent = 'Undone';
      setTimeout(() => render(activeRoute, true), 12000); // the history refreshes when main moves
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Undo';
      const node = document.querySelector('.project-tab-body > p.subtle');
      if (node) node.textContent = `Could not undo: ${error.message}`;
    }
  };
  registerClick('undoJob', undo);
  registerClick('undoCommit', undo);
  registerClick('envDelete', async button => {
    const name = button.dataset.envDelete;
    if (!globalThis.confirm?.(`Delete ${name}? The app restarts without it.`)) return;
    try {
      await requestJSON(`/api/projects/${encodeURIComponent(activeRoute.projectId)}/env/${encodeURIComponent(name)}`, { method: 'DELETE' });
      await render(activeRoute, true);
    } catch (error) {
      button.textContent = error.message;
    }
  });
  registerClick('restoreTrash', async button => {
    button.disabled = true;
    button.textContent = 'Restoring…';
    try {
      const request_id = requestId();
      const response = await requestJSON(`/api/projects-trash/${encodeURIComponent(button.dataset.restoreTrash)}/restore`, {
        method: 'POST',
        body: JSON.stringify({ request_id })
      });
      const result = await poll(response.request_id || request_id);
      if (result.status !== 'succeeded') throw new Error(resultMessage(result));
      await render(activeRoute, true);
    } catch (error) {
      button.disabled = false;
      button.textContent = `Restore failed: ${error.message}`;
    }
  });
  registerClick('appRestart', async button => {
    button.disabled = true;
    try {
      await requestJSON(`/api/projects/${encodeURIComponent(button.dataset.appRestart)}/app/restart`, { method: 'POST' });
      button.textContent = 'Restarting…';
    } catch (error) {
      button.textContent = error.message;
    }
  });
  registerClick('retryClone', async button => {
    const id = button.dataset.retryClone;
    try {
      const request_id = requestId();
      const response = await requestJSON(`/api/projects/${encodeURIComponent(id)}/retry-clone`, {
        method: 'POST',
        body: JSON.stringify({ request_id })
      });
      const result = await poll(response.request_id || request_id);
      status('project-push-status', resultMessage(result), result.output);
    } catch (error) {
      status('project-push-status', error.message);
    }
  });
  root()?.addEventListener('change', async event => {
    if (event.target.id !== 'project-importance') return;
    try {
      await requestJSON(`/api/projects/${encodeURIComponent(event.target.dataset.project)}`, {
        method: 'PATCH',
        body: JSON.stringify({ importance: event.target.value })
      });
      await render(activeRoute, true);
    } catch (error) {
      const target = root().querySelector('.form-status');
      if (target) target.textContent = error.message;
    }
  });
  root()?.addEventListener('submit', async event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    event.preventDefault();
    const values = formValues(form);
    try {
      if (form.id === 'new-project-form') {
        const error = validateProjectValues(values);
        if (error) return status('new-project-status', error);
        const body = buildProjectRequest(values);
        const response = await requestJSON('/api/projects', { method: 'POST', body: JSON.stringify(body) });
        const result = await poll(response.request_id || body.request_id);
        status('new-project-status', resultMessage(result), result.output);
      } else if (form.id === 'project-goal-form') {
        const response = await requestJSON(`/api/projects/${encodeURIComponent(activeRoute.projectId)}/goals`, {
          method: 'POST',
          body: JSON.stringify({ goal: values.goal?.trim(), atomic: values.atomic === 'on', request_id: requestId() })
        });
        status('project-goal-status', `Submitted goal ${response.id}`);
      } else if (form.id === 'project-env-form') {
        const name = text(values.name, '').trim();
        if (!name) return status('project-env-status', 'Enter a name');
        await requestJSON(`/api/projects/${encodeURIComponent(activeRoute.projectId)}/env`, {
          method: 'PUT',
          body: JSON.stringify({ name, value: values.value ?? '' })
        });
        form.reset();
        document.activeElement?.blur?.();
        status('project-env-status', `Saved ${name}; the app restarts with it`);
        await render(activeRoute, true);
      } else if (form.id === 'project-settings-form') {
        const body = buildSettingsRequest(values);
        if (body.run_port !== undefined && !(body.run_port >= 8100 && body.run_port <= 8199))
          return status('project-settings-status', 'Port must be between 8100 and 8199');
        await requestJSON(`/api/projects/${encodeURIComponent(activeRoute.projectId)}`, { method: 'PATCH', body: JSON.stringify(body) });
        status('project-settings-status', 'Saved');
        document.activeElement?.blur?.();
        await render(activeRoute, true);
      } else if (form.id === 'project-delete-form') {
        const id = activeRoute.projectId;
        if (text(values.confirm, '').trim() !== id) return status('project-delete-status', `Type ${id} exactly to confirm`);
        busy = true;
        status('project-delete-status', 'Deleting…');
        const request_id = requestId();
        const response = await requestJSON(`/api/projects/${encodeURIComponent(id)}/delete`, {
          method: 'POST',
          body: JSON.stringify({ confirm: id, request_id })
        });
        const result = await poll(response.request_id || request_id);
        busy = false;
        if (result.status === 'succeeded') location.hash = '#/projects';
        else status('project-delete-status', resultMessage(result));
      } else if (form.id === 'project-push-form') {
        const request_id = requestId();
        const response = await requestJSON(`/api/projects/${encodeURIComponent(activeRoute.projectId)}/push-setup`, {
          method: 'POST',
          body: JSON.stringify({ url: values.url?.trim(), request_id })
        });
        const result = await poll(response.request_id || request_id);
        status('project-push-status', resultMessage(result), result.output);
      }
    } catch (error) {
      busy = false;
      const target = form.querySelector('.form-status');
      if (target) target.innerHTML = esc(error.message);
    }
  });
}
