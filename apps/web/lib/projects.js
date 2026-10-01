import { requestJSON, operatorRequest, newRequestId } from './api.js';
import { esc, escValue, pill, text, number } from './format.js';
import { registerPanel, registerClick, onRoute } from './registry.js';

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

export function projectDetailMarkup(project) {
  const id = text(project?.id, '');
  const name = text(project?.name, '');
  const goals = Array.isArray(project?.goals) ? project.goals : [];
  const jobs = Array.isArray(project?.jobs) ? project.jobs : [];
  const retry = project?.status === 'pending_key'
    ? `<button type="button" data-retry-clone="${esc(id)}">Retry clone</button><span class="subtle">Add the deploy key shown when the project was created, then retry</span>`
    : '';
  const goalItems = goals.map(goal => {
    const progress = goal.progress || {};
    return `<div class="item"><div class="item-head"><span class="item-title">${esc(goal.summary ?? goal.prompt)}</span>${pill(
      goal.status
    )}</div><div class="subtle">${esc(number(progress.completed))}/${esc(number(progress.total))} jobs</div></div>`;
  }).join('');
  const jobRows = jobs.map(job => `<tr><td><button type="button" data-detail="${esc(job.id)}">${esc(job.id)}</button></td><td>${pill(
    job.status
  )}</td><td>${esc(job.review_verdict)}</td><td>${esc(job.provider)}/${esc(job.model)}</td></tr>`).join('');
  return `<section class="panel wide"><div class="panel-heading"><div><p class="eyebrow">${id === 'sid' ? 'SID · THIS SYSTEM' : 'PROJECT'}</p><h2>${esc(name)}</h2></div>${id && id !== 'sid' ? `<a class="button" href="#/projects/${encodeURIComponent(id)}/files">Files ↓</a>` : ''}<label>Importance <select id="project-importance" data-project="${esc(id)}"><option value="high"${
    project.importance === 'high' ? ' selected' : ''
  }>high</option><option value="medium"${project.importance === 'medium' ? ' selected' : ''}>medium</option><option value="low"${
    project.importance === 'low' ? ' selected' : ''
  }>low</option></select></label></div><div class="stack">${pill(project.status)}${retry}</div><form id="project-goal-form" class="goal-form"><textarea name="goal" placeholder="Describe work for ${esc(
    name
  )}" required></textarea><label><input type="checkbox" name="atomic"> atomic</label><button type="submit">Submit goal</button><span id="project-goal-status" class="form-status" role="status"></span></form>${id === 'sid' ? systemInfoMarkup(project.system) : '<form id="project-push-form" class="goal-form"><div class="form-row"><input name="url" placeholder="GitHub repository URL" required><button type="submit">Set up GitHub push</button></div><span id="project-push-status" class="form-status" role="status"></span></form>'}<div class="stack"><h3>Goals</h3>${goalItems || '<div class="empty">No goals yet</div>'}</div><div class="table-wrap"><table class="job-table"><thead><tr><th>Job</th><th>Status</th><th>Review</th><th>Provider/model</th></tr></thead><tbody>${jobRows}</tbody></table></div>${buildSettingsMarkup(project)}${deleteProjectMarkup(id)}</section>`;
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
        if (version === renderVersion) container.innerHTML = `<section class="panel wide"><div class="panel-heading"><div><p class="eyebrow">PROJECTS</p><h2>Projects</h2></div><div><a class="button" href="#/projects/new">Create a project</a></div></div><div class="stack">${projectListMarkup(projects)}</div></section>${trashMarkup(trashed)}`;
      } catch (error) {
        container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
      }
      return;
    }
    try {
      const project = await requestJSON(`/api/projects/${encodeURIComponent(route.projectId)}?limit=25`);
      if (version === renderVersion) detailMain().innerHTML = projectDetailMarkup(project);
    } catch (error) {
      if (version !== renderVersion) return;
      detailMain().innerHTML = `<div class="empty">${error.message === 'HTTP 404' ? 'Project not found' : esc(error.message)}</div>`;
    }
  }
  const formValues = form => Object.fromEntries(new FormData(form).entries());
  onRoute(route => {
    activeRoute = route;
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
    if (route.view !== 'projects' || route.create) return;
    if (route.projectId) detailMain(); // before the file browser looks for its slot
    render(route, true);
    refreshTimer = setInterval(() => render(activeRoute), 5000);
  });
  registerPanel(() => {
    if (activeRoute?.view === 'projects' && !refreshTimer) render(activeRoute);
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
