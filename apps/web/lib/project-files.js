// Project file browser, on every project page (below the details;
// #/projects/<id>/files scrolls to it): browse and download a
// project's code (main) and app data; upload to data, or to code as a commit
// on main made by the host (apps/api/file_routes.py).
import { requestJSON, operatorRequest, newRequestId } from './api.js';
import { esc, escValue } from './format.js';
import { registerClick, onRoute } from './registry.js';

const AREAS = {
  code: { label: 'Code (main)', note: 'Every change here (upload, rename, move, copy, zip, delete) is committed straight to main as you: no review, no tests.' },
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

// The actions a row offers. Download sends a file as itself and a folder
// as a zip; links (rare) can only be renamed, moved or deleted.
export function rowActions(entry) {
  const actions = [];
  if (entry.type !== 'link') actions.push(['download', entry.type === 'dir' ? 'Download as zip' : 'Download']);
  actions.push(['rename', 'Rename…'], ['move', 'Move to…']);
  if (entry.type !== 'link') actions.push(['copy', 'Copy to…'], ['zip', 'Zip']);
  if (entry.type === 'file' && /\.zip$/i.test(entry.name)) actions.push(['unzip', 'Unzip']);
  actions.push(['delete', 'Delete']);
  return actions;
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
    const options = rowActions(entry)
      .map(([value, label]) => `<option value="${value}">${esc(label)}</option>`)
      .join('');
    const menu = `<select class="file-action" data-file-action="${escValue(full)}" data-file-type="${escValue(entry.type)}" aria-label="Actions for ${escValue(entry.name)}"><option value="">Actions…</option>${options}</select>`;
    return `<tr><td>${name}</td><td>${escValue(formatSize(entry.size))}</td><td class="subtle">${escValue(modified)}</td><td>${menu}</td></tr>`;
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
  const newFolder = '<button type="button" data-file-new-folder="1">New folder</button>';
  return `<section class="panel wide" id="project-files"><div class="panel-heading"><div><p class="eyebrow">FILES</p><h2>Code and data</h2></div><div class="file-tabs">${tabs}</div></div><p class="subtle">${esc(AREAS[area].note)}</p>${breadcrumbMarkup(
    area,
    path
  )}<div class="form-row file-actions"><label class="button">Upload files<input type="file" id="project-file-input" multiple hidden></label>${newFolder}<a class="button" href="${escValue(
    downloadUrl(projectId, area, path)
  )}" download>Download this folder</a></div><div id="project-file-status" class="form-status" role="status">${escValue(state.message)}</div>${body}</section>`;
}

let state = null;
let listing = null;
const root = () => (typeof document === 'undefined' ? null : document.getElementById('project-files-panel'));

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
const base = () => `/api/projects/${encodeURIComponent(state.projectId)}/files/${state.area}`;

// One file operation. Data: done at once. Code: the host commits it to main.
async function runOp(op, path, dest = '') {
  const area = state.area;
  const body = { op, path, dest };
  if (area === 'code') body.request_id = newRequestId();
  say(area === 'code' ? `Committing ${op} of ${path || dest} to main…` : `Working…`);
  try {
    const response = await requestJSON(`${base()}/op`, { method: 'POST', body: JSON.stringify(body) });
    if (area === 'code') {
      const result = await waitForHost(response.request_id || body.request_id);
      say(result.status === 'succeeded' ? `Done: ${result.message || op}` : `${op} failed: ${result.message || result.status}`);
    } else {
      say(`Done: ${op} ${response.path || path}`);
    }
  } catch (error) {
    say(error.message);
  }
  await load();
}

function download(path) {
  const link = document.createElement('a');
  link.href = downloadUrl(state.projectId, state.area, path);
  link.download = '';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

const promptFolder = (verb, name) =>
  globalThis.prompt?.(`${verb} ${name} to which folder? (path from the top, empty = top level)`, state.path);

async function rowAction(select) {
  const path = select.dataset.fileAction;
  const action = select.value;
  select.value = '';
  if (!state || !action) return;
  const name = path.split('/').pop();
  if (action === 'download') return download(path);
  if (action === 'rename') {
    const next = globalThis.prompt?.(`Rename ${name} to`, name)?.trim();
    if (next && next !== name) await runOp('rename', path, next);
  } else if (action === 'move' || action === 'copy') {
    const dest = promptFolder(action === 'move' ? 'Move' : 'Copy', name);
    if (dest !== null && dest !== undefined) await runOp(action, path, dest.trim().replace(/^\/+|\/+$/g, ''));
  } else if (action === 'delete') {
    const where = state.area === 'code' ? 'the code (this commits to main)' : 'the app data';
    if (globalThis.confirm?.(`Delete ${name} from ${where}? This cannot be undone.`)) await runOp('delete', path);
  } else if (action === 'zip' || action === 'unzip') {
    await runOp(action, path);
  }
}

registerClick('fileNewFolder', async () => {
  if (!state) return;
  const name = globalThis.prompt?.('New folder name')?.trim();
  if (name) await runOp('mkdir', joinPath(state.path, name));
});

if (typeof document !== 'undefined') {
  document.addEventListener('change', event => {
    if (event.target?.matches?.('select[data-file-action]')) return rowAction(event.target);
    if (event.target?.id !== 'project-file-input' || !state) return;
    const files = Array.from(event.target.files || []);
    if (files.length) upload(files);
  });
}

const scrollToFiles = () => root()?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });

onRoute(route => {
  if (!(route.view === 'projects' && route.projectId && !route.create) || route.projectId === 'sid') {
    state = null;
    return;
  }
  if (state?.projectId === route.projectId && root()?.firstChild) {
    if (route.files) scrollToFiles();
    return;
  }
  state = { projectId: route.projectId, area: 'code', path: '', message: '' };
  load().then(() => route.files && scrollToFiles());
});
