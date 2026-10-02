import {
  ENDPOINTS,
  HISTORY_PAGE_SIZE,
  dismiss,
  fetchEndpoint,
  fetchHistoryPage,
  fetchJobDetail,
  handoffText,
  jobAction,
  loadDismissals,
  newRequestId,
  operatorRequest,
  operatorToken,
  removeWorker,
  requestJSON,
  submitGoal,
  workerAction
} from './lib/api.js';
import { TERMINAL, asArray, asObject, duration, esc, number, pill, text } from './lib/format.js';
import { approvalMarkup, jobActionsMarkup, jobDetailMarkup, tokenStateText, workerMarkup } from './lib/markup.js';

import { dispatchClick, renderPanels } from './lib/registry.js';
import { applyRoute, currentRoute } from './lib/router.js';
// Feature modules register panels/click handlers (see lib/registry.js).
import './lib/features.js';

// app.js stays the public entry point: tests and callers import from here.
export * from './lib/api.js';
export * from './lib/markup.js';
export * from './lib/registry.js';
export * from './lib/router.js';

// Settings → General → Dashboard refresh (lib/appearance.js sets LAIKA_PREFS first).
const POLL_MS = Math.min(60, Math.max(2, Number(globalThis.LAIKA_PREFS?.DASHBOARD_REFRESH_SECONDS) || 2)) * 1000;
if (typeof document !== 'undefined') {
  const seconds = document.getElementById('poll-seconds');
  if (seconds) seconds.textContent = String(POLL_MS / 1000);
}
const HISTORY_REFRESH_MS = 30000;
const initial = () => Object.fromEntries(ENDPOINTS.map(key => [key, { data: null, error: null, stale: false }]));
export const state = { ...initial(), lastUpdated: null, polling: false, dismissed: new Set() };
state.goalSubmission = { pending: false, requestId: null };
state.historyOffset = 0;
state.history = { data: null, error: null, stale: false };
export async function poll(fetchImpl = fetch) {
  if (state.polling) return state;
  state.polling = true;
  try {
    (await Promise.allSettled(ENDPOINTS.map(key => fetchEndpoint(key, fetchImpl)))).forEach((result, index) => {
      const key = ENDPOINTS[index];
      state[key] =
        result.status === 'fulfilled'
          ? { data: result.value, error: null, stale: false }
          : { ...state[key], error: result.reason?.message || 'Request failed', stale: state[key].data !== null };
    });
    try {
      state.dismissed = new Set(await loadDismissals(fetchImpl));
    } catch {}
    state.lastUpdated = new Date();
    render();
  } finally {
    state.polling = false;
  }
  return state;
}
const list = (id, items, template, empty) => {
  const node = document.getElementById(id);
  if (node) node.innerHTML = items.length ? items.map(template).join('') : `<div class="empty">${empty}</div>`;
};
const active = item => !TERMINAL.test(text(item.status, '')) && !state.dismissed.has(item.id);
export function render() {
  const get = k => state[k].data,
    status = asObject(get('status')) || {},
    repo = asObject(get('repository')) || {},
    queue = asObject(get('queue')) || {},
    workers = asArray(get('workers')),
    orchestrators = asArray(get('orchestrators')),
    goals = asArray(get('goals')),
    jobs = asArray(get('jobs')),
    approvals = asArray(get('approvals')).filter(
      j => j.status === 'awaiting_review' && j.review_status === 'complete' && j.review_verdict === 'pass'
    ),
    failures = asArray(get('failures')),
    actionJobs = jobs.filter(active),
    history = asArray(state.history.data).filter(j => !state.dismissed.has(j.id));
  const signals = [
      'repository',
      'queue',
      'orchestrators',
      'workers',
      'heartbeat',
      'goals',
      'jobs',
      'approvals',
      'failures'
    ].filter(k => status[k] !== undefined),
    statusText =
      status.health ||
      status.state ||
      status.overall ||
      (signals.length
        ? signals.some(k => /fail|error|offline/i.test(text(status[k])))
          ? 'Degraded'
          : 'Operational'
        : 'Awaiting telemetry');
  document.getElementById('metric-status').textContent = text(statusText);
  document.getElementById('metric-services').textContent = signals.length
    ? `${signals.length} status signals reporting`
    : 'Awaiting telemetry';
  document.getElementById('metric-branch').textContent = text(repo.branch);
  document.getElementById('metric-repo').innerHTML = pill(repo.status);
  document.getElementById('metric-queue').textContent = number(queue.depth);
  document.getElementById('metric-queue-name').textContent = text(queue.name, 'Queue unavailable');
  document.getElementById('metric-workers').textContent = number(
    workers.filter(
      w => /^(working|busy|claimed|running|active|stopping)$/i.test(text(w.status, '')) || Boolean(w.active_job_id)
    ).length
  );
  document.getElementById('metric-orchestrators').textContent = `Orchestrators ${orchestrators.length}`;
  document.getElementById('orchestrator-count').textContent = orchestrators.length;
  document.getElementById('approval-count').textContent = approvals.length;
  const visibleFailures = failures.filter(j => !state.dismissed.has(j.id));
  document.getElementById('failure-count').textContent = visibleFailures.length;
  list(
    'orchestrators',
    orchestrators,
    o =>
      `<div class="entity"><div class="entity-head"><span class="entity-name">${esc(o.id)}</span>${pill(o.status)}</div><div class="subtle">${esc(o.provider || 'Provider')} · ${esc(o.model || 'model unknown')}</div><div class="stats"><span>goal <b>${esc(o.active_goal || 'none')}</b></span><span>heartbeat <b>${esc(o.heartbeat_age == null ? '—' : `${o.heartbeat_age}s ago`)}</b></span></div></div>`,
    'No orchestrators reporting'
  );
  list('workers', workers, w => workerMarkup(w), 'No workers reporting');
  list(
    'goals',
    goals.filter(active),
    g => {
      const p = g.progress || {},
        percent = p.total ? Math.min(100, Math.round(((p.completed || 0) / p.total) * 100)) : 0;
      return `<div class="item"><div class="item-head"><span class="item-title">${esc(g.summary || g.prompt || g.id)}</span>${pill(g.status)}</div><div class="subtle">${esc(g.id)} · ${p.completed || 0}/${p.total || 0} jobs complete${g.planner_provider ? ` · planned by ${esc(g.planner_provider)}/${esc(g.planner_model || '?')}` : ''}</div><div class="bar"><i style="width:${percent}%"></i></div><button data-handoff="${esc(g.id)}">Copy for ChatGPT</button><button data-download="${esc(g.id)}">Download handoff</button><button data-dismiss="${esc(g.id)}">Dismiss</button></div>`;
    },
    'No actionable goals'
  );
  list('approvals', approvals, approvalMarkup, 'No jobs are ready for approval');
  list(
    'failures',
    visibleFailures,
    j =>
      `<div class="item history-item"><div class="item-head"><span class="item-title">${esc(j.id)}</span>${pill(j.status)}</div><p>${esc(j.error || 'Failure reason unavailable')}</p><button data-dismiss="${esc(j.id)}">Dismiss</button></div>`,
    'No historical failures'
  );
  const table = items =>
    items.length
      ? `<table class="job-table"><thead><tr><th>Job</th><th>Status</th><th>Review</th><th>Tokens</th><th>Duration</th><th>Telemetry</th><th>Execution</th><th>Commits / Tests</th><th>Failure</th><th>Actions</th></tr></thead><tbody>${items.map(j => `<tr><td><button class="detail-button" data-detail="${esc(j.id)}">${esc(j.id)}</button></td><td>${pill(j.status)}</td><td>${esc(j.review_status || '—')}</td><td>${number(j.effective_tokens)}</td><td>${duration(j.duration)}</td><td>${esc(j.provider || '—')} · ${esc(j.model || '—')} · ${esc(j.worker || '—')}</td><td>commands ${number(j.command_count)} · files ${number(j.files)}</td><td>base ${esc(j.base || '—')}<br>candidate ${esc(j.candidate || '—')}<br>tests ${esc(j.tests || '—')}</td><td>${esc(j.error || '—')}</td><td>${jobActionsMarkup(j)}</td></tr>`).join('')}</tbody></table>`
      : '<div class="empty">No jobs found</div>';
  document.getElementById('jobs').innerHTML = table(actionJobs);
  document.getElementById('history').innerHTML = table(history);
  document.querySelector('[data-dismiss-all="failures"]').disabled = visibleFailures.length === 0;
  document.querySelector('[data-dismiss-all="history"]').disabled = history.length === 0;
  const historyRange = document.getElementById('history-range'),
    historyNewer = document.querySelector('[data-history="newer"]'),
    historyOlder = document.querySelector('[data-history="older"]');
  if (historyRange) {
    const start = state.historyOffset + 1,
      end = state.historyOffset + history.length;
    historyRange.textContent = `jobs ${start}-${end}`;
  }
  if (historyNewer) historyNewer.disabled = state.historyOffset === 0;
  if (historyOlder) historyOlder.disabled = history.length < HISTORY_PAGE_SIZE;
  const errors = ENDPOINTS.filter(k => state[k].error),
    banner = document.getElementById('banner');
  banner.hidden = !errors.length;
  banner.textContent = errors.length
    ? `Partial telemetry: ${errors.map(k => `${k} (${state[k].error})`).join(' · ')}. Showing last known data where available.`
    : '';
  document.getElementById('live-dot').classList.toggle('offline', errors.length === ENDPOINTS.length);
  document.getElementById('last-updated').textContent = state.lastUpdated
    ? `Updated ${state.lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
    : 'Connecting…';
  document.getElementById('poll-state').textContent = errors.length
    ? `${errors.length} endpoint${errors.length === 1 ? '' : 's'} degraded`
    : 'All endpoints healthy';
  renderPanels(state);
}
async function loadHistoryPage(offset = state.historyOffset) {
  try {
    const data = await fetchHistoryPage(offset);
    state.historyOffset = offset;
    state.history = { data, error: null, stale: false };
  } catch (error) {
    state.history = { ...state.history, error: error.message || 'Request failed', stale: state.history.data !== null };
  }
  render();
}
export async function copyHandoff(
  goalId,
  fetchImpl = fetch,
  clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : null
) {
  const value = await handoffText(goalId, fetchImpl);
  const fallback = () => {
    const area = document.createElement('textarea');
    area.value = value;
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  };
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(value);
    } catch {
      fallback();
    }
  } else fallback();
  return value;
}
export async function downloadHandoff(goalId, fetchImpl = fetch) {
  const response = await fetchImpl(`/api/goals/${encodeURIComponent(goalId)}/handoff`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob(),
    url = URL.createObjectURL(blob),
    link = document.createElement('a');
  link.href = url;
  link.download = `handoff-${goalId}.zip`;
  link.click();
  URL.revokeObjectURL(url);
}
async function uiAction(fn, target) {
  try {
    await fn();
    if (target) {
      target.hidden = false;
      target.textContent = 'Done';
    }
    await poll();
    return true;
  } catch (error) {
    if (target) {
      target.hidden = false;
      target.textContent = error.message;
    }
    return false;
  }
}
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function runJobAction(button) {
  const banner = document.getElementById('banner'),
    operation = button.dataset.op,
    jobId = button.dataset.job,
    candidate = button.dataset.candidate;
  if (operation === 'approve' && !window.confirm(`Approve candidate ${candidate} for job ${jobId}?`)) return;
  button.disabled = true;
  const request_id = newRequestId(),
    body = {
      action: operation,
      request_id,
      expected_status: button.dataset.status,
      ...(operation === 'approve' ? { expected_candidate: candidate } : {}),
      ...(operation === 'extend' ? { extra: 1 } : {})
    };
  try {
    await jobAction(jobId, body);
    let result = await operatorRequest(request_id),
      attempts = 0;
    while (/^(pending|running)$/i.test(text(result.status, '')) && attempts < 30) {
      await wait(POLL_MS);
      result = await operatorRequest(request_id);
      attempts += 1;
    }
    await poll();
    banner.hidden = false;
    banner.textContent = `${text(result.status, 'completed')}: ${text(result.message, '')}`.replace(/: $/, '');
  } catch (error) {
    await poll();
    banner.hidden = false;
    banner.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
if (typeof document !== 'undefined') {
  applyRoute(currentRoute());
  window.addEventListener('hashchange', () => applyRoute(currentRoute()));
  render();
  poll();
  loadHistoryPage();
  setInterval(() => poll(), POLL_MS);
  setInterval(() => loadHistoryPage(), HISTORY_REFRESH_MS);
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    dispatchClick(button, event);
    if (button.dataset.detailClose) {
      document.getElementById('job-detail').hidden = true;
      return;
    }
    if (button.dataset.detail) {
      fetchJobDetail(button.dataset.detail)
        .then(job => {
          const section = document.getElementById('job-detail');
          document.getElementById('job-detail-content').innerHTML = jobDetailMarkup(job);
          section.hidden = false;
          section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        })
        .catch(error => {
          const banner = document.getElementById('banner');
          banner.hidden = false;
          banner.textContent = error.message || 'Unable to load job detail';
        });
      return;
    }
    if (button.dataset.dismiss || button.dataset.dismissAll) {
      const panelId = button.dataset.dismissAll,
        ids =
          panelId === 'history'
            ? asArray(state.history.data)
                .map(job => job.id)
                .filter(id => id && !state.dismissed.has(id))
                .slice(0, 500)
            : panelId
              ? [...document.querySelectorAll(`#${panelId} [data-dismiss]`)]
                  .map(item => item.dataset.dismiss)
                  .slice(0, 500)
              : [button.dataset.dismiss];
      if (!ids.length) return;
      button.disabled = true;
      try {
        await dismiss(ids);
        ids.forEach(id => state.dismissed.add(id));
        render();
      } catch (error) {
        const banner = document.getElementById('banner');
        banner.hidden = false;
        banner.textContent = error.message;
        button.disabled = false;
      }
    }
    if (button.dataset.action) {
      const id = button.dataset.worker,
        action = button.dataset.action;
      uiAction(
        () => (action === 'remove' ? removeWorker(id) : workerAction(id, action)),
        document.getElementById('banner')
      );
    }
    if (button.dataset.op) runJobAction(button);
    if (button.dataset.handoff) uiAction(() => copyHandoff(button.dataset.handoff), document.getElementById('banner'));
    if (button.dataset.download)
      uiAction(() => downloadHandoff(button.dataset.download), document.getElementById('banner'));
    if (button.dataset.history) {
      const offset = Math.max(
        0,
        state.historyOffset + (button.dataset.history === 'older' ? HISTORY_PAGE_SIZE : -HISTORY_PAGE_SIZE)
      );
      loadHistoryPage(offset);
    }
  });
  document.getElementById('goal-form')?.addEventListener('submit', event => {
    event.preventDefault();
    if (state.goalSubmission.pending) return;
    const input = document.getElementById('goal-input'),
      result = document.getElementById('goal-result'),
      button = event.target.querySelector('button[type="submit"]');
    state.goalSubmission.pending = true;
    state.goalSubmission.requestId = state.goalSubmission.requestId || newRequestId();
    button.disabled = true;
    result.textContent = 'Submitting…';
    uiAction(
      () => submitGoal(input.value, document.getElementById('atomic-input').checked, state.goalSubmission.requestId),
      result
    ).then(success => {
      state.goalSubmission.pending = false;
      button.disabled = false;
      if (success) {
        state.goalSubmission.requestId = null;
        input.value = '';
      }
    });
  });
}
