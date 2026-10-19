// "Why is this waiting?" (apps/api/why_routes.py): a Why? button on home
// cards and a line at the top of a job's details.
import { requestJSON } from './api.js';
import { registerClick } from './registry.js';

async function reasonFor(jobId) {
  const answer = await requestJSON(`/api/jobs/${encodeURIComponent(jobId)}/why`);
  return answer.waiting ? answer.reason : `Not waiting: ${answer.reason}.`;
}

if (typeof document !== 'undefined') {
  registerClick('why', async button => {
    const id = button.dataset.why;
    const target = document.querySelector(`[data-why-text="${CSS.escape(id)}"]`);
    if (!target) return;
    target.textContent = '…';
    try {
      target.textContent = await reasonFor(id);
    } catch (error) {
      target.textContent = error.message;
    }
  });
  window.addEventListener('laika:detail-shown', async event => {
    const target = document.querySelector(`[data-why-auto="${CSS.escape(String(event.detail))}"]`);
    if (!target) return;
    try {
      const answer = await requestJSON(`/api/jobs/${encodeURIComponent(event.detail)}/why`);
      target.textContent = answer.waiting ? `Why it's waiting: ${answer.reason}` : '';
    } catch {
      target.textContent = '';
    }
  });
}
