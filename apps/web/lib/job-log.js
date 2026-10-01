// Live job log in the job detail panel: polls GET /api/jobs/<id>/log for new
// events (agent messages, commands and their output, file changes, the
// result) while the panel is open, and keeps polling while the job runs.
import { requestJSON } from './api.js';
import { esc } from './format.js';
import { registerClick } from './registry.js';

const LABELS = { message: 'says', command: 'runs', output: 'output', error: 'error', files: 'changes', result: 'done' };

export function logLineMarkup(event) {
  const kind = LABELS[event?.kind] ? event.kind : 'output';
  const exit = event.exit_code !== undefined && event.exit_code !== null ? ` (exit ${esc(event.exit_code)})` : '';
  const body = kind === 'output' || kind === 'error' ? `<pre>${esc(event.text)}</pre>` : `<span>${esc(event.text)}</span>`;
  return `<div class="log-line log-${kind}"><span class="log-kind">${LABELS[kind]}${exit}</span>${body}</div>`;
}

const followers = new Map(); // job id -> {offset, timer}

async function poll(element) {
  const id = element.dataset.jobLog;
  const state = followers.get(id) || { offset: 0, started: false };
  followers.set(id, state);
  try {
    const result = await requestJSON(`/api/jobs/${encodeURIComponent(id)}/log?after=${state.offset}`);
    if (!state.started) {
      element.innerHTML = result.available ? '' : '<div class="subtle">No log for this job (yet).</div>';
      state.started = true;
    }
    if (result.events?.length) {
      const atBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 24;
      element.insertAdjacentHTML('beforeend', result.events.map(logLineMarkup).join(''));
      if (atBottom) element.scrollTop = element.scrollHeight;
    }
    state.offset = result.next ?? state.offset;
    state.running = result.running;
  } catch (error) {
    if (!state.started) element.innerHTML = `<div class="subtle">${esc(error.message)}</div>`;
    state.started = true;
  }
}

// One loop for whatever log panels are on screen; stops following closed ones.
function tick() {
  const open = new Set();
  document.querySelectorAll('[data-job-log]').forEach(element => {
    open.add(element.dataset.jobLog);
    const state = followers.get(element.dataset.jobLog);
    if (!state || !state.started || state.running || !state.finalFetched) {
      if (state && state.started && !state.running) state.finalFetched = true;
      poll(element);
    }
  });
  for (const id of followers.keys()) if (!open.has(id)) followers.delete(id);
}

if (typeof document !== 'undefined') {
  setInterval(tick, 2000);
  new MutationObserver(() => {
    if (document.querySelector('[data-job-log]') && [...document.querySelectorAll('[data-job-log]')].some(e => !followers.has(e.dataset.jobLog))) tick();
  }).observe(document.body, { childList: true, subtree: true });
}

registerClick('jobLogTests', async button => {
  const pre = button.parentElement?.querySelector('.job-test-log');
  if (!pre) return;
  if (!pre.hidden) {
    pre.hidden = true;
    button.textContent = 'Show test output';
    return;
  }
  try {
    const result = await requestJSON(`/api/jobs/${encodeURIComponent(button.dataset.jobLogTests)}/log?tests=true`);
    pre.textContent = result.available ? result.events.map(event => event.text).join('') || '(empty)' : 'No test output for this job.';
  } catch (error) {
    pre.textContent = error.message;
  }
  pre.hidden = false;
  button.textContent = 'Hide test output';
});
