import { requestJSON, jobAction, newRequestId } from './api.js';
import { esc, pill, text } from './format.js';
import { registerPanel, registerClick } from './registry.js';

let latestQueue = null;
let lastState = null;
let started = false;

export function mergeQueueMarkup(queue) {
  if (!Array.isArray(queue?.items) || !queue.items.length) return '<div class="empty">Merge queue is empty</div>';

  const value = item => esc(text(item, ''));

  const rows = queue.items
    .map(
      item => `<tr>
        <td>${value(item.position)}</td>
        <td><button type="button" data-detail="${value(item.id)}">${value(item.id)}</button></td>
        <td>${value(item.title)}</td>
        <td>${pill(text(item.state, ''))}</td>
        <td>${value(item.reason)}</td>
        <td>${item.fresh ? 'yes' : 'no'}</td>
        <td><button type="button" data-dequeue="${value(item.id)}" data-status="${value(item.status)}">Remove</button></td>
      </tr>`
    )
    .join('');

  return `<div class="table-wrap"><table class="job-table">
    <thead><tr><th>#</th><th>Job</th><th>Title</th><th>State</th><th>Reason</th><th>Fresh</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

const readyApprovals = approvals =>
  (Array.isArray(approvals) ? approvals : []).filter(approval => approval?.integrated_candidate_commit);

export function queueControlsMarkup(approvals) {
  const ready = readyApprovals(approvals);
  if (!ready.length) return '';

  const value = item => esc(text(item, ''));

  return `<div class="actions">${ready
    .map(
      approval =>
        `<button type="button" data-queue-approve="${value(approval.id)}" data-candidate="${value(
          approval.integrated_candidate_commit
        )}" data-status="${value(approval.status)}">Approve when ready ${value(approval.id)}</button>`
    )
    .join('')}<button type="button" data-queue-all>Queue all ready (${ready.length})</button></div>`;
}

async function refreshQueue() {
  try {
    latestQueue = await requestJSON('/api/merge-queue');
  } catch {}
  renderMergeQueue(lastState);
}

function renderMergeQueue(state) {
  if (typeof document === 'undefined') return;
  if (state !== undefined) lastState = state;

  let panel = document.getElementById('merge-queue-panel');
  if (!panel) {
    const grid = document.querySelector('.dashboard-grid');
    if (!grid) return;
    panel = document.createElement('section');
    panel.className = 'panel wide';
    panel.id = 'merge-queue-panel';
    grid.append(panel);
  }

  panel.innerHTML = `<div class="panel-heading"><div><p class="eyebrow">MERGE QUEUE</p><h2>Approved changes waiting to merge</h2></div></div>
    <div class="stack">${queueControlsMarkup(lastState?.approvals?.data)}${mergeQueueMarkup(latestQueue)}</div>`;
}

const showBanner = message => {
  if (typeof document === 'undefined') return;
  const banner = document.getElementById('banner');
  if (banner) {
    banner.hidden = false;
    banner.textContent = message;
  }
};

const formatResult = (id, result) => {
  if (result && typeof result === 'object') {
    const suffix = [result.message, result.detail, result.reason].filter(Boolean).join(' ');
    if (result.status || suffix) return `${id}: ${result.status || 'submitted'}${suffix ? ` ${suffix}` : ''}`;
  }
  return `${id}: ${JSON.stringify(result)}`;
};

const formatError = (id, error) => `${id}: ${error?.message || error}`;

if (typeof document !== 'undefined') {
  registerPanel(state => {
    renderMergeQueue(state);
    if (!started) {
      started = true;
      refreshQueue();
      setInterval(refreshQueue, 5000);
    }
  });

  registerClick('queueApprove', async button => {
    const { queueApprove: id, candidate, status } = button.dataset;
    if (!window.confirm(`Queue ${id} for merge at candidate ${candidate}?`)) return;
    try {
      showBanner(
        formatResult(
          id,
          await jobAction(id, {
            action: 'queue_approve',
            request_id: newRequestId(),
            expected_status: status,
            expected_candidate: candidate
          })
        )
      );
    } catch (error) {
      showBanner(formatError(id, error));
    }
    await refreshQueue();
  });

  registerClick('queueAll', async () => {
    const ready = readyApprovals(lastState?.approvals?.data);
    const buttons = document.querySelectorAll('[data-queue-approve]');
    const jobs = ready.length
      ? ready.map(({ id, integrated_candidate_commit: candidate, status }) => ({ id, candidate, status }))
      : [...buttons].map(button => ({
          id: button.dataset.queueApprove,
          candidate: button.dataset.candidate,
          status: button.dataset.status
        }));
    if (!jobs.length) return;
    const lines = jobs.map(job => `${job.id} ${job.candidate}`).join('\n');
    if (!window.confirm(`Queue these jobs for merge?\n${lines}`)) return;

    const results = [];
    for (const { id, candidate, status } of jobs) {
      try {
        results.push(
          formatResult(
            id,
            await jobAction(id, {
              action: 'queue_approve',
              request_id: newRequestId(),
              expected_status: status,
              expected_candidate: candidate
            })
          )
        );
      } catch (error) {
        results.push(formatError(id, error));
      }
    }
    showBanner(results.join('\n'));
    await refreshQueue();
  });

  registerClick('dequeue', async button => {
    const { dequeue: id, status } = button.dataset;
    try {
      showBanner(
        formatResult(
          id,
          await jobAction(id, {
            action: 'dequeue_approve',
            request_id: newRequestId(),
            expected_status: status
          })
        )
      );
    } catch (error) {
      showBanner(formatError(id, error));
    }
    await refreshQueue();
  });
}
