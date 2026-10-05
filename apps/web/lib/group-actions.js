// Project groups, phase 3: approve everything a goal produced (through the
// merge queue, apps/api/group_routes.py), a whole group's activity, and
// building every member at once.
import { requestJSON, newRequestId, operatorRequest } from './api.js';
import { esc, escValue } from './format.js';
import { registerClick } from './registry.js';

// Changes just sent to the merge queue: until the next refresh reports their
// merge-queue state, the page must not offer them again (or the status line
// vanishes in a re-render and the button looks like it did nothing).
const justQueued = new Map();
const QUEUED_FOR_MS = 120000;
export function markQueued(jobIds, now = Date.now()) {
  for (const id of jobIds) justQueued.set(id, now);
}
export function recentlyQueued(job, now = Date.now()) {
  const at = justQueued.get(job?.id);
  if (at === undefined) return false;
  if (now - at > QUEUED_FOR_MS || /^(queued|waiting)$/.test(job.merge_queue_state || '') || job.status === 'merged') {
    justQueued.delete(job.id);
    return false;
  }
  return true;
}
const stillOpen = job => !recentlyQueued(job) && !/^(queued|waiting)$/.test(job.merge_queue_state || '');

// Goals with two or more changes ready to approve.
export function readyByGoal(ready = []) {
  const goals = new Map();
  for (const job of ready.filter(stillOpen)) {
    if (!job.goal_id || !/^[0-9a-f]{40}$/.test(job.integrated_candidate_commit || '')) continue;
    if (!goals.has(job.goal_id)) goals.set(job.goal_id, []);
    goals.get(job.goal_id).push(job);
  }
  return [...goals.entries()].filter(([, jobs]) => jobs.length > 1);
}

export function approveAllMarkup(ready = []) {
  return readyByGoal(ready)
    .map(([goalId, jobs]) => {
      const candidates = Object.fromEntries(jobs.map(job => [job.id, job.integrated_candidate_commit]));
      const titles = jobs.map(job => `<li>${esc(job.title || job.id)} <code>${esc(job.integrated_candidate_commit.slice(0, 8))}</code></li>`).join('');
      return `<article class="home-card attention approve-all-card"><div class="card-top"><span class="card-state ok">${esc(jobs.length)} changes from one goal</span></div><ul class="approve-all-list">${titles}</ul><p class="subtle">Approve them all: each joins the merge queue and is merged one after another, re-checked by the tests and a fresh review whenever main moves.</p><div class="card-actions"><button type="button" class="approve-button" data-approve-all="${escValue(goalId)}" data-candidates="${escValue(JSON.stringify(candidates))}">Approve all ${esc(jobs.length)}</button></div><span class="form-status" role="status"></span></article>`;
    })
    .join('');
}

// A parent project's page: every open change of the group at once, when they
// come from more than one goal (one goal already has its own card).
export function approveGroupMarkup(project, ready = []) {
  const open = ready.filter(job => /^[0-9a-f]{40}$/.test(job.integrated_candidate_commit || '') && stillOpen(job));
  if (!project?.children?.length || open.length < 2 || new Set(open.map(job => job.goal_id)).size < 2) return '';
  const candidates = Object.fromEntries(open.map(job => [job.id, job.integrated_candidate_commit]));
  const titles = open.map(job => `<li>${esc(job.title || job.id)} <span class="subtle">· ${esc(job.project_id)}</span> <code>${esc(job.integrated_candidate_commit.slice(0, 8))}</code></li>`).join('');
  return `<article class="home-card attention approve-all-card"><div class="card-top"><span class="card-state ok">${esc(open.length)} changes across the group</span></div><ul class="approve-all-list">${titles}</ul><p class="subtle">Approve every ready change in ${esc(project.name || project.id)} and its child projects: each joins the merge queue and is merged one after another, re-checked by the tests and a fresh review whenever main moves.</p><div class="card-actions"><button type="button" class="approve-button" data-approve-group="${escValue(project.id)}" data-candidates="${escValue(JSON.stringify(candidates))}">Approve all ${esc(open.length)}</button></div><span class="form-status" role="status"></span></article>`;
}

export const inGroup = project => Boolean(project?.parent || project?.children?.length);

export function groupToggleMarkup(project, whole) {
  if (!inGroup(project)) return '';
  return `<div class="segmented group-toggle"><button type="button" data-activity-group="0"${whole ? '' : ' class="active" aria-pressed="true"'}>This project</button><button type="button" data-activity-group="1"${whole ? ' class="active" aria-pressed="true"' : ''}>Whole group</button></div>`;
}

export function buildAllMarkup(project, viewOnly) {
  if (!inGroup(project) || viewOnly) return '';
  return `<button type="button" data-build-all="${escValue(project.id)}">Build the whole group</button>`;
}

let wholeGroup = false;
try {
  wholeGroup = globalThis.sessionStorage?.getItem('laika-activity-group') === '1';
} catch {}
export const wholeGroupActivity = () => wholeGroup;

if (typeof document !== 'undefined') {
  const status = (button, message) => {
    const node = button.closest('article, .builds, .card')?.querySelector('.form-status');
    if (node) node.textContent = message;
    else window.alert(message);
  };
  // Queue them, wait for the operator service to take each one, then refresh
  // the page and say what happened in the page banner (which survives refreshes).
  const approveListed = async (button, url) => {
    let candidates = {};
    try {
      candidates = JSON.parse(button.dataset.candidates || '{}');
    } catch {}
    const ids = Object.keys(candidates);
    if (!window.confirm(`Approve all ${ids.length} changes? Each goes through the merge queue and is merged only after its tests and a fresh review pass on the latest main.`)) return;
    button.disabled = true;
    status(button, 'Sending…');
    let message;
    try {
      const result = await requestJSON(url, { method: 'POST', body: JSON.stringify({ request_id: newRequestId().slice(0, 40), candidates }) });
      markQueued(ids);
      const outcomes = await Promise.all(result.queued.map(async item => {
        if (item.error || !item.request_id) return { ...item, final: 'error' };
        for (let tries = 0; tries < 30; tries += 1) {
          const answer = await operatorRequest(item.request_id).catch(() => ({}));
          if (answer.status && !/^(pending|running)$/.test(answer.status)) return { ...item, final: answer.status, message: answer.message };
          await new Promise(resolve => setTimeout(resolve, 1500));
        }
        return { ...item, final: 'pending' };
      }));
      const bad = outcomes.filter(item => !/^(succeeded|pending)$/.test(item.final));
      if (bad.length) justQueued.clear();
      message = bad.length
        ? `Queued ${ids.length - bad.length} of ${ids.length}; not queued: ${bad.map(item => `${item.job_id} (${item.error || item.message || item.final})`).join('; ')}`
        : `All ${ids.length} changes are in the merge queue; each merges by itself after its tests and a fresh review pass.`;
    } catch (error) {
      const problems = error.body?.detail?.problems;
      message = problems ? `Nothing approved: ${problems.join('; ')}` : error.message;
      button.disabled = false;
    }
    status(button, message);
    window.dispatchEvent(new CustomEvent('laika:notice', { detail: message }));
    window.dispatchEvent(new CustomEvent('laika:poll-now'));
    window.dispatchEvent(new CustomEvent('laika:project-refresh'));
  };
  registerClick('approveAll', button => approveListed(button, `/api/goals/${encodeURIComponent(button.dataset.approveAll)}/approve-all`));
  registerClick('approveGroup', button => approveListed(button, `/api/projects/${encodeURIComponent(button.dataset.approveGroup)}/approve-all`));
  registerClick('activityGroup', button => {
    wholeGroup = button.dataset.activityGroup === '1';
    try {
      sessionStorage.setItem('laika-activity-group', wholeGroup ? '1' : '0');
    } catch {}
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  });
  registerClick('buildAll', async button => {
    button.disabled = true;
    try {
      const result = await requestJSON(`/api/projects/${encodeURIComponent(button.dataset.buildAll)}/builds/all`, { method: 'POST' });
      const skipped = result.skipped.map(item => `${item.id}: ${item.reason}`).join('; ');
      status(button, `Building ${result.started.map(item => item.id).join(', ')}.${skipped ? ` Skipped: ${skipped}` : ''}`);
    } catch (error) {
      const detail = error.body?.detail;
      status(button, detail?.skipped ? `${detail.message}: ${detail.skipped.map(item => `${item.id}: ${item.reason}`).join('; ')}` : error.message);
    }
    button.disabled = false;
  });
}
