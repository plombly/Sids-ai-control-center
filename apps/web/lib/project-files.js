// Project file browser, on every project page (below the details;
// #/projects/<id>/files scrolls to it). Works like a desktop file manager:
// click / Ctrl+click / Shift+click to select, right-click (long-press on
// touch) for actions, Ctrl+X/C/V to cut, copy and paste (across folders and
// between Code and App data), drag onto a folder or breadcrumb to move (hold
// Ctrl or Option to copy), drop files from your computer to upload.
// Changes to Code are committed to main by the host (apps/api/file_routes.py).
import { authHeaders, errorMessage, operatorRequest, newRequestId } from './api.js';
import { esc, escValue } from './format.js';
import { onRoute } from './registry.js';
import { askConfirm, askConflicts, askFolder, askText, entryBadge } from './file-dialogs.js';

export const AREA_LABELS = { code: 'Code (main)', data: 'App data' };
const AREA_NOTES = {
  code: 'Every change here is committed straight to main as you: no review, no tests.',
  data: "Your app's data folder (DATA_DIR). Changes here are immediate and not part of the code."
};
const DRAG_TYPE = 'application/x-laika-files';

// --- pure helpers (tested in project-files.test.js) ---------------------------------------

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

export function downloadUrl(projectId, area, paths) {
  const query = new URLSearchParams({ area });
  for (const path of [].concat(paths)) query.append('path', path || '');
  return `/api/projects/${encodeURIComponent(projectId)}/files/download?${query}`;
}

// Desktop-style selection. names: the listing order. Returns the new
// selection (array, in listing order) and the anchor for the next Shift+click.
export function selectClick(selected, names, index, { ctrl = false, shift = false } = {}, anchor = null) {
  const name = names[index];
  if (shift && anchor !== null && names[anchor] !== undefined) {
    const [from, to] = anchor < index ? [anchor, index] : [index, anchor];
    const base = ctrl ? new Set(selected) : new Set();
    names.slice(from, to + 1).forEach(item => base.add(item));
    return { selected: names.filter(item => base.has(item)), anchor };
  }
  if (ctrl) {
    const next = new Set(selected);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    return { selected: names.filter(item => next.has(item)), anchor: index };
  }
  return { selected: [name], anchor: index };
}

// Context-menu entries for a selection (entries) or for the background.
export function menuItems({ entries = [], clipboard = null }) {
  const canPaste = Boolean(clipboard && clipboard.names?.length);
  if (!entries.length) {
    const what = canPaste ? (clipboard.names.length === 1 ? clipboard.names[0] : `${clipboard.names.length} items`) : '';
    return [
      ...(canPaste ? [['paste', `Paste ${what} here`]] : []),
      ['mkdir', 'New folder'],
      ['upload', 'Upload files'],
      ['select-all', 'Select all']
    ];
  }
  const single = entries.length === 1 ? entries[0] : null;
  const hasLink = entries.some(entry => entry.type === 'link');
  const items = [];
  if (single?.type === 'dir') items.push(['open', 'Open']);
  if (!hasLink) items.push(['download', single && single.type === 'file' ? 'Download' : 'Download as zip']);
  if (single) items.push(['rename', 'Rename']);
  items.push('-', ['cut', 'Cut'], ['copy', 'Copy']);
  if (single?.type === 'dir' && canPaste) items.push(['paste-into', 'Paste into this folder']);
  items.push(['move-to', 'Move to…'], ['copy-to', 'Copy to…'], '-');
  if (!hasLink) items.push(['zip', entries.length === 1 ? 'Zip' : `Zip ${entries.length} items`]);
  if (single?.type === 'file' && /\.zip$/i.test(single.name)) items.push(['unzip', 'Unzip']);
  items.push('-', ['delete', entries.length === 1 ? 'Delete' : `Delete ${entries.length} items`]);
  return items;
}

// May these paths be dropped/pasted into dest? (Never a folder into itself.)
export function canDrop(sourcePaths, sourceArea, dest, destArea) {
  if (sourceArea !== destArea) return true;
  return !sourcePaths.some(source => dest === source || dest.startsWith(`${source}/`));
}

// The batch request for pasting the clipboard into area/dest.
export function pasteRequest(clip, area, dest) {
  if (!clip?.names?.length) return null;
  return {
    op: clip.mode === 'cut' ? 'move' : 'copy',
    from_area: clip.area,
    to_area: area,
    paths: clip.names.map(name => joinPath(clip.folder, name)),
    dest
  };
}

export function breadcrumbMarkup(area, path) {
  const parts = path ? path.split('/') : [];
  const crumb = (target, label) =>
    `<button type="button" class="detail-button" data-file-nav="${escValue(target)}" data-drop-path="${escValue(target)}">${esc(label)}</button>`;
  const crumbs = [crumb('', AREA_LABELS[area])];
  parts.forEach((part, index) => crumbs.push(crumb(parts.slice(0, index + 1).join('/'), part)));
  return `<div class="breadcrumb">${crumbs.join('<span class="subtle"> / </span>')}</div>`;
}

export function listingMarkup(state) {
  const { entries = [], selected = [], clipboard: clip = null, area, path, projectId } = state;
  const chosen = new Set(selected);
  const cutHere =
    clip?.mode === 'cut' && clip.projectId === projectId && clip.area === area && clip.folder === path ? new Set(clip.names) : new Set();
  if (!entries.length) return `<div class="empty file-drop" data-drop-path="${escValue(path)}">This folder is empty. Drop files here to upload.</div>`;
  const rows = entries.map(entry => {
    const classes = ['file-row', chosen.has(entry.name) ? 'selected' : '', cutHere.has(entry.name) ? 'cut' : ''].filter(Boolean).join(' ');
    const drop = entry.type === 'dir' ? ` data-drop-path="${escValue(joinPath(path, entry.name))}"` : '';
    const modified = entry.modified ? new Date(entry.modified * 1000).toLocaleString() : '';
    return `<tr class="${classes}" data-name="${escValue(entry.name)}" data-type="${escValue(entry.type)}" draggable="true"${drop} aria-selected="${chosen.has(entry.name)}"><td class="file-name-cell">${entryBadge(entry)}</td><td>${escValue(formatSize(entry.size))}</td><td class="subtle">${escValue(modified)}</td></tr>`;
  });
  return `<div class="table-wrap file-drop-zone" data-drop-path="${escValue(path)}"><table class="job-table file-table"><thead><tr><th>Name</th><th>Size</th><th>Modified</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

export function clipboardNote(clip) {
  if (!clip?.names?.length) return '';
  const what = clip.names.length === 1 ? clip.names[0] : `${clip.names.length} items`;
  return `${clip.mode === 'cut' ? 'Cut' : 'Copied'}: ${esc(what)} (${esc(AREA_LABELS[clip.area])}). Ctrl+V or right-click to paste.`;
}

// The bar's space is always there (a hint when nothing is selected), so
// selecting never shifts the list under the pointer.
export function selectionBarMarkup(count) {
  if (!count) return '<div class="selection-hint subtle">Click to select · Ctrl/⌘ or Shift for more · double-click to open · right-click for actions</div>';
  return `<div class="selection-bar"><span>${count} selected</span><button type="button" data-file-cmd="download">Download</button><button type="button" data-file-cmd="zip">Zip</button><button type="button" data-file-cmd="cut">Cut</button><button type="button" data-file-cmd="copy">Copy</button><button type="button" class="danger-button" data-file-cmd="delete">Delete</button><button type="button" class="detail-button" data-file-cmd="clear">Clear</button></div>`;
}

export function filesPageMarkup(state, listing) {
  const { area, path, message, clipboard: clip } = state;
  const tabs = Object.entries(AREA_LABELS)
    .map(([key, label]) => `<button type="button" class="${key === area ? 'active' : ''}" data-file-area="${key}">${esc(label)}</button>`)
    .join('');
  const clipNote = `<span class="subtle clipboard-note">${clipboardNote(clip)}</span>`;
  const body = listing?.error
    ? `<div class="empty">${esc(listing.error)}</div>`
    : listing
      ? listingMarkup({ ...state, entries: listing.entries })
      : '<div class="empty">Loading…</div>';
  return `<section class="panel wide file-browser" id="project-files"><div class="panel-heading"><div><p class="eyebrow">FILES</p><h2>Code and data</h2></div><div class="file-tabs">${tabs}</div></div><p class="subtle">${esc(
    AREA_NOTES[area]
  )} Right-click for actions.</p>${breadcrumbMarkup(area, path)}<div class="form-row file-actions"><label class="button">Upload files<input type="file" id="project-file-input" multiple hidden></label><button type="button" data-file-cmd="mkdir">New folder</button><button type="button" data-file-cmd="download-folder">Download this folder</button>${clipNote}</div><div class="selection-slot">${selectionBarMarkup(
    state.selected?.length || 0
  )}</div><div id="project-file-status" class="form-status" role="status">${escValue(message)}</div>${body}</section>`;
}

// --- state ------------------------------------------------------------------------------------

let view = null; // {projectId, area, path, selected, anchor, message}
let listing = null;
let clipboard = null; // {projectId, area, folder, names, mode}: survives navigation
let focused = false; // keyboard shortcuts only while the browser was last used
const root = () => (typeof document === 'undefined' ? null : document.getElementById('project-files-panel'));
const api = suffix => `/api/projects/${encodeURIComponent(view.projectId)}/files${suffix}`;
const entries = () => listing?.entries || [];
const selectedEntries = () => entries().filter(entry => view.selected.includes(entry.name));
const here = names => names.map(name => joinPath(view.path, name));
const projectClipboard = () => (clipboard?.projectId === view?.projectId ? clipboard : null);
const plural = (n, word = 'item') => `${n} ${word}${n === 1 ? '' : 's'}`;

function render() {
  const container = root();
  if (container && view) container.innerHTML = filesPageMarkup({ ...view, clipboard: projectClipboard() }, listing);
}

// Selection, cut marks and the clipboard note, updated in place: rows are
// not redrawn, so a double-click still lands on the same element.
function paint({ bar = true } = {}) {
  const container = root();
  if (!container || !view) return;
  const chosen = new Set(view.selected);
  const clip = projectClipboard();
  const cut = clip?.mode === 'cut' && clip.area === view.area && clip.folder === view.path ? new Set(clip.names) : new Set();
  container.querySelectorAll('tr.file-row').forEach(row => {
    row.classList.toggle('selected', chosen.has(row.dataset.name));
    row.classList.toggle('cut', cut.has(row.dataset.name));
    row.setAttribute('aria-selected', String(chosen.has(row.dataset.name)));
  });
  if (!bar) return; // mid-drag: nothing may move under the pointer
  const slot = container.querySelector('.selection-slot');
  if (slot) slot.innerHTML = selectionBarMarkup(view.selected.length);
  const note = container.querySelector('.clipboard-note');
  if (note) note.innerHTML = clipboardNote(clip);
}

async function call(url, options = {}) {
  const json = options.body && typeof options.body === 'string';
  const response = await fetch(url, {
    ...options,
    headers: { accept: 'application/json', ...(json ? { 'content-type': 'application/json' } : {}), ...authHeaders(), ...(options.headers || {}) }
  });
  let body = {};
  try {
    body = await response.json();
  } catch {}
  return { ok: response.ok, status: response.status, body };
}

async function load(keepSelection = false) {
  if (!view) return;
  const query = new URLSearchParams({ area: view.area, path: view.path });
  try {
    const response = await call(`${api('')}?${query}`);
    listing = response.ok ? response.body : { error: errorMessage(response.body, response.status) };
  } catch (error) {
    listing = { error: error.message };
  }
  const names = new Set(entries().map(entry => entry.name));
  view.selected = keepSelection ? view.selected.filter(name => names.has(name)) : [];
  if (!keepSelection) view.anchor = null;
  render();
}

function say(message) {
  if (!view) return;
  view.message = message;
  const node = document.getElementById('project-file-status');
  if (node) node.textContent = message;
}

async function waitForHost(requestId) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const result = await operatorRequest(requestId);
    if (!/^(pending|running)$/.test(String(result?.status || ''))) return result;
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  return { status: 'expired', message: 'The host did not answer in time' };
}

// Name clashes the host found (a race after the API's own check).
export function hostConflicts(message) {
  const match = /conflict: (\[.*\])/.exec(String(message || ''));
  try {
    return match ? JSON.parse(match[1]) : null;
  } catch {
    return null;
  }
}

// One batch; asks about name clashes until they are answered or cancelled.
async function runBatch(request, working) {
  const attempt = { ...request, resolutions: { ...(request.resolutions || {}) } };
  const target = attempt.to_area || attempt.from_area;
  for (let round = 0; round < 5; round += 1) {
    attempt.request_id = newRequestId();
    say(working);
    const response = await call(api('/batch'), { method: 'POST', body: JSON.stringify(attempt) });
    let conflicts = response.status === 409 ? response.body?.detail?.conflicts : null;
    if (!conflicts && response.status === 202) {
      const result = await waitForHost(response.body.request_id || attempt.request_id);
      if (result.status === 'succeeded') return true;
      conflicts = hostConflicts(result.message);
      if (!conflicts) {
        say(`Not done: ${result.message || result.status}`);
        return false;
      }
    } else if (!conflicts) {
      if (response.ok) return true;
      say(errorMessage(response.body, response.status));
      return false;
    }
    const answer = await askConflicts(conflicts, target);
    if (!answer) {
      say('Cancelled');
      return false;
    }
    Object.assign(attempt.resolutions, answer.resolutions);
    if (attempt.op === 'zip' || attempt.op === 'rename') attempt.default = answer.resolutions[conflicts[0]];
  }
  say('Gave up after repeated name clashes');
  return false;
}

async function singleOp(op, path, dest = '') {
  const body = { op, path, dest, request_id: newRequestId() };
  say(view.area === 'code' ? 'Committing to main…' : 'Working…');
  const response = await call(api(`/${view.area}/op`), { method: 'POST', body: JSON.stringify(body) });
  if (!response.ok) {
    say(errorMessage(response.body, response.status));
    return false;
  }
  if (response.status === 202) {
    const result = await waitForHost(body.request_id);
    if (result.status !== 'succeeded') {
      say(`Not done: ${result.message || result.status}`);
      return false;
    }
  }
  return true;
}

async function finish(ok, message) {
  if (ok) say(message);
  await load(!ok);
}

const working = (...areas) => (areas.includes('code') ? 'Committing to main…' : 'Working…');

// --- actions ---------------------------------------------------------------------------------

function download(names) {
  const link = document.createElement('a');
  link.href = downloadUrl(view.projectId, view.area, names.length ? here(names) : [view.path]);
  link.download = '';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function uploadFiles(files, destPath = view.path) {
  if (!files.length) return;
  const query = new URLSearchParams({ area: view.area });
  files.forEach(file => query.append('path', joinPath(destPath, file.name)));
  const existing = (await call(`${api('/exists')}?${query}`)).body?.exists || [];
  let choices = {};
  if (existing.length) {
    const answer = await askConflicts(existing.map(item => item.split('/').pop()), view.area);
    if (!answer) return say('Upload cancelled');
    choices = answer.resolutions;
  }
  let index = 0;
  for (const file of files) {
    index += 1;
    say(`Uploading ${file.name} (${index}/${files.length})…`);
    const params = new URLSearchParams({ path: joinPath(destPath, file.name), on_conflict: choices[file.name] || 'ask' });
    if (view.area === 'code') params.set('request_id', newRequestId());
    const response = await call(`${api(`/${view.area}`)}?${params}`, { method: 'PUT', body: file, headers: { 'content-type': 'application/octet-stream' } });
    if (!response.ok) {
      say(`${file.name}: ${errorMessage(response.body, response.status)}`);
      return load(true);
    }
    if (view.area === 'code') {
      say(`Committing ${file.name} to main…`);
      const result = await waitForHost(response.body.request_id || params.get('request_id'));
      if (result.status !== 'succeeded') {
        say(`${file.name}: ${result.message || result.status}`);
        return load(true);
      }
    }
  }
  await finish(true, `Uploaded ${plural(files.length, 'file')}`);
}

function setClipboard(mode) {
  if (!view.selected.length) return;
  clipboard = { projectId: view.projectId, area: view.area, folder: view.path, names: [...view.selected], mode };
  say(`${mode === 'cut' ? 'Cut' : 'Copied'} ${plural(view.selected.length)}`);
  paint();
}

async function paste(dest = view.path) {
  const clip = projectClipboard();
  if (!clip) return say('Nothing to paste: cut or copy something first');
  const request = pasteRequest(clip, view.area, dest);
  if (!canDrop(request.paths, clip.area, dest, view.area)) return say('Cannot paste a folder into itself');
  if (request.op === 'move' && clip.area === view.area && clip.folder === dest) return say('Already here');
  const ok = await runBatch(request, working(view.area, request.op === 'move' ? clip.area : ''));
  if (ok && clip.mode === 'cut') clipboard = null;
  await finish(ok, `Pasted ${plural(request.paths.length)}`);
}

async function transfer(mode, sources, fromArea, dest, destArea) {
  if (!canDrop(sources, fromArea, dest, destArea)) return say('Cannot put a folder inside itself');
  if (mode === 'move' && fromArea === destArea && sources.every(source => (source.includes('/') ? source.slice(0, source.lastIndexOf('/')) : '') === dest)) return;
  const request = { op: mode, from_area: fromArea, to_area: destArea, paths: sources, dest };
  const ok = await runBatch(request, working(destArea, mode === 'move' ? fromArea : ''));
  await finish(ok, `${mode === 'move' ? 'Moved' : 'Copied'} ${plural(sources.length)}`);
}

async function listFolders(area, path) {
  const response = await call(`${api('')}?${new URLSearchParams({ area, path })}`);
  return (response.body?.entries || []).filter(entry => entry.type === 'dir').map(entry => entry.name);
}

function startRename(name) {
  const row = root()?.querySelector(`tr[data-name="${CSS.escape(name)}"]`);
  const cell = row?.querySelector('.file-name-cell');
  if (!cell) return;
  row.draggable = false;
  cell.innerHTML = `<input class="rename-input" value="${escValue(name)}" aria-label="New name">`;
  const input = cell.querySelector('input');
  input.focus();
  const dot = name.lastIndexOf('.');
  if (dot > 0 && row.dataset.type === 'file') input.setSelectionRange(0, dot);
  else input.select();
  let ended = false;
  const end = async commit => {
    if (ended) return;
    ended = true;
    const next = input.value.trim();
    if (!commit || !next || next === name) return render();
    const ok = await runBatch({ op: 'rename', from_area: view.area, paths: [joinPath(view.path, name)], dest: next }, working(view.area));
    await finish(ok, `Renamed to ${next}`);
  };
  input.addEventListener('keydown', event => {
    event.stopPropagation();
    if (event.key === 'Enter') end(true);
    if (event.key === 'Escape') end(false);
  });
  input.addEventListener('blur', () => end(true));
}

function navigate(path) {
  closeMenu();
  view.path = path;
  view.message = '';
  listing = null;
  render();
  load();
}

async function command(action) {
  if (!view) return;
  const chosen = selectedEntries();
  const names = chosen.map(entry => entry.name);
  const single = chosen.length === 1 ? chosen[0] : null;
  switch (action) {
    case 'open':
      if (single?.type === 'dir') navigate(joinPath(view.path, single.name));
      else if (single?.type === 'file') download([single.name]);
      return;
    case 'download':
      if (names.length) download(names);
      return;
    case 'download-folder':
      return download([]);
    case 'rename':
      if (single) startRename(single.name);
      return;
    case 'cut':
    case 'copy':
      return setClipboard(action);
    case 'paste':
      return paste(view.path);
    case 'paste-into':
      if (single?.type === 'dir') await paste(joinPath(view.path, single.name));
      return;
    case 'move-to':
    case 'copy-to': {
      if (!names.length) return;
      const move = action === 'move-to';
      const where = await askFolder({
        title: `${move ? 'Move' : 'Copy'} ${names.length === 1 ? names[0] : plural(names.length)} to…`,
        area: view.area,
        path: view.path,
        areas: ['code', 'data'],
        labels: AREA_LABELS,
        listFolders,
        confirm: move ? 'Move here' : 'Copy here'
      });
      if (where) await transfer(move ? 'move' : 'copy', here(names), view.area, where.path, where.area);
      return;
    }
    case 'zip': {
      if (!names.length) return;
      const name = await askText({ title: 'Zip', label: 'Archive name', value: single ? `${single.name}.zip` : 'Archive.zip', confirm: 'Zip', selectStem: true });
      if (!name) return;
      const ok = await runBatch({ op: 'zip', from_area: view.area, paths: here(names), dest: view.path, name }, working(view.area));
      return finish(ok, `Created ${name.toLowerCase().endsWith('.zip') ? name : `${name}.zip`}`);
    }
    case 'unzip':
      if (single) await finish(await singleOp('unzip', joinPath(view.path, single.name)), `Unzipped ${single.name}`);
      return;
    case 'delete': {
      if (!names.length) return;
      const where = view.area === 'code' ? 'the code (committed to main; still in git history)' : 'the app data (permanent)';
      const yes = await askConfirm({
        title: `Delete ${names.length === 1 ? names[0] : plural(names.length)}?`,
        message: `This deletes from ${where}.`,
        items: names,
        confirm: 'Delete',
        danger: true
      });
      if (yes) await finish(await runBatch({ op: 'delete', from_area: view.area, paths: here(names) }, working(view.area)), `Deleted ${plural(names.length)}`);
      return;
    }
    case 'mkdir': {
      const name = await askText({ title: 'New folder', label: 'Folder name', value: 'New folder', confirm: 'Create' });
      if (name) await finish(await singleOp('mkdir', joinPath(view.path, name)), `Created ${name}`);
      return;
    }
    case 'upload':
      document.getElementById('project-file-input')?.click();
      return;
    case 'select-all':
      view.selected = entries().map(entry => entry.name);
      return paint();
    case 'clear':
      view.selected = [];
      return paint();
  }
}

// --- context menu --------------------------------------------------------------------------

let menu = null;

function closeMenu() {
  menu?.remove();
  menu = null;
}

function openMenu(x, y) {
  closeMenu();
  const items = menuItems({ entries: selectedEntries(), clipboard: projectClipboard() });
  menu = document.createElement('div');
  menu.className = 'file-menu';
  menu.setAttribute('role', 'menu');
  menu.innerHTML = items
    .map(item => (item === '-' ? '<hr>' : `<button type="button" role="menuitem" data-menu="${item[0]}"${item[0] === 'delete' ? ' class="danger"' : ''}>${esc(item[1])}</button>`))
    .join('');
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(4, Math.min(x, window.innerWidth - rect.width - 4))}px`;
  menu.style.top = `${Math.max(4, Math.min(y, window.innerHeight - rect.height - 4))}px`;
  menu.querySelector('button')?.focus({ preventScroll: true });
  menu.addEventListener('click', event => {
    const button = event.target.closest('[data-menu]');
    if (!button) return;
    closeMenu();
    command(button.dataset.menu);
  });
  menu.addEventListener('keydown', event => {
    const buttons = Array.from(menu.querySelectorAll('button'));
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    }
    if (event.key === 'Escape') closeMenu();
  });
}

// --- events ----------------------------------------------------------------------------------

const inBrowser = target => Boolean(target && root()?.contains(target));
const rowOf = target => target?.closest?.('tr.file-row') || null;
const indexOf = row => entries().findIndex(entry => entry.name === row.dataset.name);

function selectRow(row, event) {
  const names = entries().map(entry => entry.name);
  const result = selectClick(view.selected, names, indexOf(row), { ctrl: event.ctrlKey || event.metaKey, shift: event.shiftKey }, view.anchor);
  view.selected = result.selected;
  view.anchor = result.anchor;
  paint();
}

function contextAt(target, x, y) {
  const row = rowOf(target);
  if (row && !view.selected.includes(row.dataset.name)) {
    view.selected = [row.dataset.name];
    view.anchor = indexOf(row);
    paint();
  } else if (!row) {
    view.selected = [];
    paint();
  }
  focused = true;
  openMenu(x, y);
}

function wire() {
  if (typeof document === 'undefined' || wire.done) return;
  wire.done = true;

  document.addEventListener('pointerdown', event => {
    if (menu && !menu.contains(event.target)) closeMenu();
    focused = inBrowser(event.target);
  });
  window.addEventListener('resize', closeMenu);
  document.addEventListener('scroll', closeMenu, true);

  document.addEventListener('click', event => {
    if (!view || !inBrowser(event.target)) return;
    const nav = event.target.closest('[data-file-nav]');
    if (nav) return navigate(nav.dataset.fileNav);
    const tab = event.target.closest('[data-file-area]');
    if (tab) {
      view.area = tab.dataset.fileArea === 'data' ? 'data' : 'code';
      view.anchor = null;
      return navigate('');
    }
    const cmd = event.target.closest('[data-file-cmd]');
    if (cmd) return command(cmd.dataset.fileCmd);
    const row = rowOf(event.target);
    if (row) {
      if (event.target.closest('input')) return;
      return selectRow(row, event);
    }
    if (!event.target.closest('button, input, label, a, select') && view.selected.length) {
      view.selected = [];
      paint();
    }
  });

  document.addEventListener('dblclick', event => {
    const row = view && inBrowser(event.target) ? rowOf(event.target) : null;
    if (!row || event.target.closest('input')) return;
    view.selected = [row.dataset.name];
    command('open');
  });

  document.addEventListener('contextmenu', event => {
    if (!view || !inBrowser(event.target) || event.target.closest('input')) return;
    event.preventDefault();
    contextAt(event.target, event.clientX, event.clientY);
  });

  // Long-press on touch screens opens the same menu.
  let press = null;
  document.addEventListener('pointerdown', event => {
    if (event.pointerType !== 'touch' || !view || !inBrowser(event.target)) return;
    const { clientX: x, clientY: y, target } = event;
    press = { x, y, timer: setTimeout(() => contextAt(target, x, y), 550) };
  });
  const cancelPress = event => {
    if (!press) return;
    if (event.type === 'pointermove' && Math.hypot(event.clientX - press.x, event.clientY - press.y) < 10) return;
    clearTimeout(press.timer);
    press = null;
  };
  ['pointerup', 'pointercancel', 'pointermove'].forEach(type => document.addEventListener(type, cancelPress));

  document.addEventListener('keydown', event => {
    if (!view || !focused || document.querySelector('dialog[open]')) return;
    if (event.target.closest?.('input, textarea, select') || (menu && menu.contains(event.target))) return;
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    const run = action => {
      event.preventDefault();
      closeMenu();
      command(action);
    };
    if (event.key === 'Escape') return run('clear');
    if (mod && key === 'a') return run('select-all');
    if (mod && key === 'c') return view.selected.length && run('copy');
    if (mod && key === 'x') return view.selected.length && run('cut');
    if (mod && key === 'v') return run('paste');
    if (event.key === 'Delete' || (event.key === 'Backspace' && event.metaKey)) return view.selected.length && run('delete');
    if (event.key === 'F2') return view.selected.length === 1 && run('rename');
    if (event.key === 'Enter') return view.selected.length === 1 && run('open');
  });

  document.addEventListener('change', event => {
    if (event.target?.id !== 'project-file-input' || !view) return;
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    uploadFiles(files);
  });

  // Drag and drop: rows onto folders and breadcrumbs, files from the desktop.
  document.addEventListener('dragstart', event => {
    const row = view && inBrowser(event.target) ? rowOf(event.target) : null;
    if (!row) return;
    if (!view.selected.includes(row.dataset.name)) {
      view.selected = [row.dataset.name];
      view.anchor = indexOf(row);
      // Only the row highlight now: showing the selection bar would shift
      // the list under the pointer and break the drag (it appears on dragend).
      paint({ bar: false });
    }
    event.dataTransfer.setData(DRAG_TYPE, JSON.stringify({ projectId: view.projectId, area: view.area, folder: view.path, names: view.selected }));
    event.dataTransfer.effectAllowed = 'copyMove';
  });
  const dropTarget = target => (view && inBrowser(target) ? target.closest('[data-drop-path]') : null);
  const clearHighlight = () => root()?.querySelectorAll('.drop-target').forEach(node => node.classList.remove('drop-target'));
  document.addEventListener('dragover', event => {
    const target = dropTarget(event.target);
    const types = Array.from(event.dataTransfer?.types || []);
    if (!target || (!types.includes(DRAG_TYPE) && !types.includes('Files'))) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = types.includes(DRAG_TYPE) ? (event.ctrlKey || event.altKey ? 'copy' : 'move') : 'copy';
    if (!target.classList.contains('drop-target')) {
      clearHighlight();
      target.classList.add('drop-target');
    }
  });
  document.addEventListener('dragleave', event => {
    if (dropTarget(event.target) && !dropTarget(event.relatedTarget)) clearHighlight();
  });
  document.addEventListener('dragend', () => {
    clearHighlight();
    paint();
  });
  document.addEventListener('drop', event => {
    const target = dropTarget(event.target);
    if (!target) return;
    event.preventDefault();
    clearHighlight();
    const dest = target.dataset.dropPath;
    let payload = null;
    try {
      payload = JSON.parse(event.dataTransfer.getData(DRAG_TYPE) || 'null');
    } catch {}
    if (payload?.projectId === view.projectId && payload.names?.length) {
      const copy = event.ctrlKey || event.altKey;
      transfer(copy ? 'copy' : 'move', payload.names.map(name => joinPath(payload.folder, name)), payload.area, dest, view.area);
      return;
    }
    const files = Array.from(event.dataTransfer.files || []);
    if (files.length) uploadFiles(files, dest);
  });
}

const scrollToFiles = () => root()?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });

onRoute(route => {
  wire();
  closeMenu();
  if (!(route.view === 'projects' && route.projectId && !route.create) || route.projectId === 'laika') {
    view = null;
    return;
  }
  if (view?.projectId === route.projectId && root()?.firstChild) {
    if (route.files) scrollToFiles();
    return;
  }
  view = { projectId: route.projectId, area: 'code', path: '', selected: [], anchor: null, message: '' };
  listing = null;
  render();
  load().then(() => route.files && scrollToFiles());
});
