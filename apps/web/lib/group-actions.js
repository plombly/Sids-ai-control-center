// Project groups, phase 3: approve everything a goal produced (through the
// merge queue, apps/api/group_routes.py), a whole group's activity, and
// building every member at once.
import { requestJSON, newRequestId } from './api.js';
import { esc, escValue } from './format.js';
import { registerClick } from './registry.js';

// Goals with two or more changes ready to approve.
export function readyByGoal(ready = []) {
  const goals = new Map();
  for (const job of ready) {
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
  const open = ready.filter(job => /^[0-9a-f]{40}$/.test(job.integrated_candidate_commit || '') && !/^(queued|waiting)$/.test(job.merge_queue_state || ''));
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
  registerClick('approveAll', async button => {
    let candidates = {};
    try {
      candidates = JSON.parse(button.dataset.candidates || '{}');
    } catch {}
    const count = Object.keys(candidates).length;
    if (!window.confirm(`Approve all ${count} changes? Each goes through the merge queue and is merged only after its tests and a fresh review pass on the latest main.`)) return;
    button.disabled = true;
    try {
      const result = await requestJSON(`/api/goals/${encodeURIComponent(button.dataset.approveAll)}/approve-all`, {
        method: 'POST',
        body: JSON.stringify({ request_id: newRequestId().slice(0, 40), candidates })
      });
      const failed = result.queued.filter(item => item.error);
      status(button, failed.length ? `Queued ${count - failed.length}; not queued: ${failed.map(f => `${f.job_id} (${f.error})`).join(', ')}` : `All ${count} queued for merging.`);
    } catch (error) {
      const problems = error.body?.detail?.problems;
      status(button, problems ? `Nothing approved: ${problems.join('; ')}` : error.message);
      button.disabled = false;
    }
  });
  registerClick('approveGroup', async button => {
    let candidates = {};
    try {
      candidates = JSON.parse(button.dataset.candidates || '{}');
    } catch {}
    const count = Object.keys(candidates).length;
    if (!window.confirm(`Approve all ${count} changes in this group? Each goes through the merge queue and is merged only after its tests and a fresh review pass on the latest main.`)) return;
    button.disabled = true;
    try {
      const result = await requestJSON(`/api/projects/${encodeURIComponent(button.dataset.approveGroup)}/approve-all`, {
        method: 'POST',
        body: JSON.stringify({ request_id: newRequestId().slice(0, 40), candidates })
      });
      const failed = result.queued.filter(item => item.error);
      status(button, failed.length ? `Queued ${count - failed.length}; not queued: ${failed.map(f => `${f.job_id} (${f.error})`).join(', ')}` : `All ${count} queued for merging.`);
    } catch (error) {
      const problems = error.body?.detail?.problems;
      status(button, problems ? `Nothing approved: ${problems.join('; ')}` : error.message);
      button.disabled = false;
    }
  });
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
