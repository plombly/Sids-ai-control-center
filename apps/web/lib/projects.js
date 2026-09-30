import { requestJSON, operatorRequest } from './api.js';
import { esc, pill, text, number } from './format.js';
import { registerPanel, registerClick, onRoute } from './registry.js';

const requestId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;

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
    )}">${esc(project.name)}</a> <span class="subtle">${esc(id)}</span></div><div>${pill(project.importance)}${
      project.status !== 'active' ? ` ${pill(project.status)}` : ''
    }</div></div><div class="subtle">${esc(countsLine)}</div><div class="subtle">remaining effort ${esc(
      number(stats.remaining_effort)
    )}</div></div>`;
  }).join('');
}

export function newProjectFormMarkup() {
  return `<form id="new-project-form" class="goal-form"><div class="form-row"><label>ID <input name="id" required></label><label>Name <input name="name" required></label><label>Importance <select name="importance"><option value="high">high</option><option value="medium" selected>medium</option><option value="low">low</option></select></label></div><div class="form-row"><label>Source <select name="source"><option value="">empty</option><option value="clone">clone</option></select></label><label>URL <input name="url"></label><label>Push remote <input name="push_remote"></label><label>Gate <input name="gate"></label><button type="submit">Create project</button></div><span id="new-project-status" class="form-status" role="status"></span></form>`;
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
  return `<section class="panel wide"><div class="panel-heading"><div><p class="eyebrow">PROJECT</p><h2>${esc(name)}</h2></div><label>Importance <select id="project-importance" data-project="${esc(id)}"><option value="high"${
    project.importance === 'high' ? ' selected' : ''
  }>high</option><option value="medium"${project.importance === 'medium' ? ' selected' : ''}>medium</option><option value="low"${
    project.importance === 'low' ? ' selected' : ''
  }>low</option></select></label></div><div class="stack">${pill(project.status)}${retry}</div><form id="project-goal-form" class="goal-form"><textarea name="goal" placeholder="Describe work for ${esc(
    name
  )}" required></textarea><label><input type="checkbox" name="atomic"> atomic</label><button type="submit">Submit goal</button><span id="project-goal-status" class="form-status" role="status"></span></form><form id="project-push-form" class="goal-form"><div class="form-row"><input name="url" placeholder="GitHub repository URL" required><button type="submit">Set up GitHub push</button></div><span id="project-push-status" class="form-status" role="status"></span></form><div class="stack"><h3>Goals</h3>${goalItems || '<div class="empty">No goals yet</div>'}</div><div class="table-wrap"><table class="job-table"><thead><tr><th>Job</th><th>Status</th><th>Review</th><th>Provider/model</th></tr></thead><tbody>${jobRows}</tbody></table></div></section>`;
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
  const root = () => document.getElementById('projects-root');
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
    if (!container || route.view !== 'projects' || route.create || (!force && focusedForm(container))) return;
    const version = ++renderVersion;
    if (route.projectId === null) {
      try {
        const projects = await requestJSON('/api/projects?limit=25');
        if (version === renderVersion) container.innerHTML = `<section class="panel wide"><div class="panel-heading"><div><p class="eyebrow">PROJECTS</p><h2>Projects</h2></div><div><a class="button" href="#/projects/new">Create a project</a></div></div><div class="stack">${projectListMarkup(projects)}</div></section>`;
      } catch (error) {
        container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
      }
      return;
    }
    try {
      const project = await requestJSON(`/api/projects/${encodeURIComponent(route.projectId)}?limit=25`);
      if (version === renderVersion) container.innerHTML = projectDetailMarkup(project);
    } catch (error) {
      if (version !== renderVersion) return;
      container.innerHTML = `<div class="empty">${error.message === 'HTTP 404' ? 'Project not found' : esc(error.message)}</div>`;
    }
  }
  const formValues = form => Object.fromEntries(new FormData(form).entries());
  onRoute(route => {
    activeRoute = route;
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
    if (route.view !== 'projects' || route.create) return;
    render(route, true);
    refreshTimer = setInterval(() => render(activeRoute), 5000);
  });
  registerPanel(() => {
    if (activeRoute?.view === 'projects' && !refreshTimer) render(activeRoute);
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
      const target = form.querySelector('.form-status');
      if (target) target.innerHTML = esc(error.message);
    }
  });
}
