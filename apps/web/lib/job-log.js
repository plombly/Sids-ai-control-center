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
  const part = element.dataset.jobLogPart || '';
  const key = part ? `${id}:${part}` : id;
  const state = followers.get(key) || { offset: 0, started: false };
  followers.set(key, state);
  try {
    const result = await requestJSON(`/api/jobs/${encodeURIComponent(id)}/log?after=${state.offset}${part ? `&part=${encodeURIComponent(part)}` : ''}`);
    // Specialist reviews: one panel per reviewer (spec, safety, ...).
    if (!part && result.parts?.length) {
      for (const name of result.parts) {
        if (!element.parentElement.querySelector(`[data-job-log-part="${name}"]`)) {
          element.insertAdjacentHTML('afterend', `<h4 class="log-part">${esc(name)} review</h4><div class="job-log" data-job-log="${esc(id)}" data-job-log-part="${esc(name)}"></div>`);
        }
      }
      if (!result.events?.length && !state.started) {
        element.hidden = true;
        state.started = true;
      }
    }
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
    const key = element.dataset.jobLogPart ? `${element.dataset.jobLog}:${element.dataset.jobLogPart}` : element.dataset.jobLog;
    open.add(key);
    const state = followers.get(key);
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
    const keyOf = e => (e.dataset.jobLogPart ? `${e.dataset.jobLog}:${e.dataset.jobLogPart}` : e.dataset.jobLog);
    if ([...document.querySelectorAll('[data-job-log]')].some(e => !followers.has(keyOf(e)))) tick();
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
