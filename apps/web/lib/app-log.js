// The running app's live log on its project page (inside the app status box
// of lib/projects.js): GET /api/projects/<id>/app/log every 3 s while the
// "Live log" section is open. The page's own redraws never wipe it: the text
// lives here and is put back after each redraw.
import { requestJSON } from './api.js';
import { esc } from './format.js';

const open = new Set(); // project ids whose log is open
const cache = new Map(); // project id -> last response

export function appLogMarkup(id) {
  return `<details class="app-log-box" data-app-log="${esc(id)}"${open.has(id) ? ' open' : ''}><summary>Live log</summary><div class="subtle app-log-meta" data-app-log-meta="${esc(id)}">${esc(metaText(cache.get(id)))}</div><pre class="app-log live" data-app-log-body="${esc(id)}">${esc(bodyText(cache.get(id)))}</pre></details>`;
}

export function metaText(data) {
  if (!data) return 'Loading…';
  const parts = [];
  if (data.since) parts.push(`running since ${data.since}`);
  parts.push(data.restarts ? `restarted ${data.restarts} time${data.restarts === 1 ? '' : 's'}` : 'no restarts');
  if (data.result && data.result !== 'success') parts.push(`last exit: ${data.result}`);
  return parts.join(' · ');
}

export const bodyText = data => (data ? (data.lines.length ? data.lines.join('\n') : 'Nothing logged yet.') : '');

function paint(id) {
  const data = cache.get(id);
  const body = document.querySelector(`[data-app-log-body="${CSS.escape(id)}"]`);
  const meta = document.querySelector(`[data-app-log-meta="${CSS.escape(id)}"]`);
  if (meta) meta.textContent = metaText(data);
  if (!body) return;
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 24;
  const text = bodyText(data);
  if (body.textContent !== text) body.textContent = text;
  if (atBottom) body.scrollTop = body.scrollHeight; // follow new lines unless you scrolled up to read
}

async function refresh(id) {
  try {
    cache.set(id, await requestJSON(`/api/projects/${encodeURIComponent(id)}/app/log`));
  } catch (error) {
    cache.set(id, { lines: [`(could not load the log: ${error.message})`], restarts: 0 });
  }
  paint(id);
}

if (typeof document !== 'undefined') {
  document.addEventListener(
    'toggle',
    event => {
      const id = event.target?.dataset?.appLog;
      if (!id) return;
      if (event.target.open) {
        open.add(id);
        refresh(id).then(() => {
          const body = document.querySelector(`[data-app-log-body="${CSS.escape(id)}"]`);
          if (body) body.scrollTop = body.scrollHeight;
        });
      } else open.delete(id);
    },
    true
  );
  setInterval(() => {
    for (const id of open) if (document.querySelector(`[data-app-log="${CSS.escape(id)}"]`)) refresh(id);
  }, 3000);
}
