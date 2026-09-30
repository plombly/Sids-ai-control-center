// Project file browser (#/projects/<id>/files): browse and download a
// project's code (main) and app data; upload to data, or to code as a commit
// on main made by the host (apps/api/file_routes.py).
import { requestJSON, operatorRequest, newRequestId } from './api.js';
import { esc, escValue } from './format.js';
import { registerClick, onRoute } from './registry.js';

const AREAS = {
  code: { label: 'Code (main)', note: 'Uploads here are committed straight to main as you (no review, no tests).' },
  data: { label: 'App data', note: "Your app's data folder (DATA_DIR). Files here are not part of the code." }
};

export function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit ? value.toFixed(1) : value} ${units[unit]}`;
}

export const joinPath = (base, name) => (base ? `${base}/${name}` : name);

export function downloadUrl(projectId, area, path) {
  const query = new URLSearchParams({ area, path: path || '' });
  return `/api/projects/${encodeURIComponent(projectId)}/files/download?${query}`;
}

export function breadcrumbMarkup(area, path) {
  const parts = path ? path.split('/') : [];
  const crumbs = [`<button type="button" class="detail-button" data-file-nav="">${esc(AREAS[area].label)}</button>`];
  parts.forEach((part, index) => {
    crumbs.push(`<button type="button" class="detail-button" data-file-nav="${escValue(parts.slice(0, index + 1).join('/'))}">${esc(part)}</button>`);
  });
  return `<div class="breadcrumb">${crumbs.join('<span class="subtle"> / </span>')}</div>`;
}

export function listingMarkup(projectId, area, path, entries = []) {
  if (!entries.length) return '<div class="empty">This folder is empty</div>';
  const rows = entries.map(entry => {
    const full = joinPath(path, entry.name);
    const name =
      entry.type === 'dir'
        ? `<button type="button" class="detail-button" data-file-nav="${escValue(full)}">${esc(entry.name)}/</button>`
        : entry.type === 'link'
          ? `<span class="subtle">${esc(entry.name)} (link)</span>`
          : `<a href="${escValue(downloadUrl(projectId, area, full))}" download>${esc(entry.name)}</a>`;
    const modified = entry.modified ? new Date(entry.modified * 1000).toLocaleString() : '';
    const remove = area === 'data' ? `<button type="button" class="danger-button" data-file-delete="${escValue(full)}">Delete</button>` : '';
    const download = entry.type === 'dir' ? `<a class="button" href="${escValue(downloadUrl(projectId, area, full))}" download>Zip</a>` : '';
    return `<tr><td>${name}</td><td>${escValue(formatSize(entry.size))}</td><td class="subtle">${escValue(modified)}</td><td>${download}${remove}</td></tr>`;
  });
  return `<div class="table-wrap"><table class="job-table file-table"><thead><tr><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

export function filesPageMarkup(state, listing) {
  const { projectId, area, path } = state;
  const tabs = Object.entries(AREAS)
    .map(([key, value]) => `<button type="button" class="${key === area ? 'active' : ''}" data-file-area="${key}">${esc(value.label)}</button>`)
    .join('');
  const body = listing?.error
    ? `<div class="empty">${esc(listing.error)}</div>`
    : listing
      ? listingMarkup(projectId, area, path, listing.entries)
      : '<div class="empty">Loading…</div>';
  const newFolder = area === 'data' ? '<button type="button" data-file-new-folder="1">New folder</button>' : '';
  return `<section class="panel wide"><div class="panel-heading"><div><p class="eyebrow">FILES</p><h2><a href="#/projects/${encodeURIComponent(
    projectId
  )}">${esc(projectId)}</a></h2></div><div class="file-tabs">${tabs}</div></div><p class="subtle">${esc(AREAS[area].note)}</p>${breadcrumbMarkup(
    area,
    path
  )}<div class="form-row file-actions"><label class="button">Upload files<input type="file" id="project-file-input" multiple hidden></label>${newFolder}<a class="button" href="${escValue(
    downloadUrl(projectId, area, path)
  )}" download>Download this folder</a></div><div id="project-file-status" class="form-status" role="status">${escValue(state.message)}</div>${body}</section>`;
}

let state = null;
let listing = null;
const root = () => (typeof document === 'undefined' ? null : document.getElementById('projects-root'));

function render() {
  const container = root();
  if (container && state) container.innerHTML = filesPageMarkup(state, listing);
}

async function load() {
  if (!state) return;
  listing = null;
  render();
  const query = new URLSearchParams({ area: state.area, path: state.path });
  try {
    listing = await requestJSON(`/api/projects/${encodeURIComponent(state.projectId)}/files?${query}`);
  } catch (error) {
    listing = { error: error.message };
  }
  render();
}

const say = message => {
  if (!state) return;
  state.message = message;
  const node = typeof document === 'undefined' ? null : document.getElementById('project-file-status');
  if (node) node.textContent = message;
};

async function waitForHost(requestId) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const result = await operatorRequest(requestId);
    if (!/^(pending|running)$/.test(String(result?.status || ''))) return result;
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  return { status: 'expired', message: 'The host did not answer in time' };
}

async function upload(files) {
  const { projectId, area, path } = state;
  const base = `/api/projects/${encodeURIComponent(projectId)}/files/${area}`;
  let index = 0;
  for (const file of files) {
    index += 1;
    const target = joinPath(path, file.name);
    say(`Uploading ${file.name} (${index}/${files.length})…`);
    const query = new URLSearchParams({ path: target });
    if (area === 'code') query.set('request_id', newRequestId());
    try {
      const response = await requestJSON(`${base}?${query}`, {
        method: 'PUT',
        body: file,
        headers: { 'content-type': 'application/octet-stream' }
      });
      if (area === 'code') {
        say(`Committing ${file.name} to main…`);
        const result = await waitForHost(response.request_id || query.get('request_id'));
        if (result.status !== 'succeeded') {
          say(`${file.name}: ${result.message || result.status}`);
          await load();
          return;
        }
      }
    } catch (error) {
      say(`${file.name}: ${error.message}`);
      await load();
      return;
    }
  }
  say(area === 'code' ? `Committed ${files.length} file(s) to main` : `Uploaded ${files.length} file(s)`);
  await load();
}

registerClick('fileNav', button => {
  if (!state) return;
  state.path = button.dataset.fileNav;
  state.message = '';
  load();
});
registerClick('fileArea', button => {
  if (!state) return;
  state.area = button.dataset.fileArea === 'data' ? 'data' : 'code';
  state.path = '';
  state.message = '';
  load();
});
registerClick('fileDelete', async button => {
  if (!state) return;
  const target = button.dataset.fileDelete;
  if (!globalThis.confirm?.(`Delete ${target} from the app data? This cannot be undone.`)) return;
  try {
    await requestJSON(`/api/projects/${encodeURIComponent(state.projectId)}/files/data?${new URLSearchParams({ path: target })}`, { method: 'DELETE' });
    say(`Deleted ${target}`);
  } catch (error) {
    say(error.message);
  }
  load();
});
registerClick('fileNewFolder', async () => {
  if (!state) return;
  const name = globalThis.prompt?.('New folder name')?.trim();
  if (!name) return;
  try {
    await requestJSON(`/api/projects/${encodeURIComponent(state.projectId)}/files/data/folder`, {
      method: 'POST',
      body: JSON.stringify({ path: joinPath(state.path, name) })
    });
    say(`Created ${name}`);
  } catch (error) {
    say(error.message);
  }
  load();
});

if (typeof document !== 'undefined') {
  document.addEventListener('change', event => {
    if (event.target?.id !== 'project-file-input' || !state) return;
    const files = Array.from(event.target.files || []);
    if (files.length) upload(files);
  });
}

onRoute(route => {
  if (!(route.view === 'projects' && route.files && route.projectId)) {
    state = null;
    return;
  }
  if (state?.projectId === route.projectId) return;
  state = { projectId: route.projectId, area: 'code', path: '', message: '' };
  load();
});
