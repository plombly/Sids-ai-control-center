// Text editor for the file browser (lib/project-files.js): opens a file in a
// full-screen dialog, saves App data at once and Code as one commit on main
// (through the same uploads as the browser, apps/api/file_routes.py). Every
// save names the version it opened (expected_sha256), so a file somebody
// else changed in the meantime is never overwritten. People with View
// access get the same editor, read-only.
import { authHeaders, errorMessage, newRequestId, operatorRequest } from './api.js';
import { esc, escValue } from './format.js';
import { fileKind } from './file-kinds.js';

export const EDIT_LIMIT = 2 * 1024 * 1024;
// Extensions that are never text, beyond the image/archive/media kinds.
const BINARY = new Set(['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'db', 'sqlite', 'sqlite3', 'exe', 'bin', 'so', 'dll', 'class', 'jar', 'woff', 'woff2', 'ttf', 'otf', 'pyc', 'apk', 'ipa', 'dmg', 'iso', 'appimage', 'wasm']);

// May this listing entry be opened in the editor?
export function isEditable(entry) {
  if (!entry || entry.type !== 'file') return false;
  if (typeof entry.size === 'number' && entry.size > EDIT_LIMIT) return false;
  const { kind } = fileKind(entry);
  if (['image', 'archive', 'media'].includes(kind)) return false;
  const name = String(entry.name || '').toLowerCase();
  const dot = name.lastIndexOf('.');
  return !BINARY.has(dot > 0 ? name.slice(dot + 1) : '');
}

// Indentation the file already uses: a tab, or N spaces (default 2).
export function indentOf(text) {
  const lines = String(text).split('\n');
  if (lines.some(line => line.startsWith('\t'))) return '\t';
  const widths = lines.map(line => /^( +)\S/.exec(line)?.[1].length).filter(Boolean);
  const smallest = widths.length ? Math.min(...widths) : 2;
  return ' '.repeat(smallest === 4 ? 4 : 2);
}

// Line and column (1-based) of a caret offset.
export function caretPosition(text, offset) {
  const before = String(text).slice(0, offset);
  const line = before.split('\n').length;
  return { line, column: offset - before.lastIndexOf('\n') };
}

export function editorMarkup({ path, area, text = '', readOnly = false, isNew = false, areaLabel = '' }) {
  const where = area === 'code' ? 'Saving commits it to main as you (no review, no tests).' : 'Saving changes the app data at once.';
  const note = readOnly ? 'Read-only: you have View access to this project.' : where;
  const lines = String(text).split('\n').length;
  return `<div class="editor-head"><div class="editor-title"><b>${esc(path)}</b>${isNew ? ' <span class="pill">new</span>' : ''}<span class="subtle"> · ${esc(areaLabel || area)}</span></div><div class="editor-actions">${
    readOnly ? '' : '<button type="button" class="primary" data-editor="save" title="Ctrl+S">Save</button>'
  }<button type="button" data-editor="close">Close</button></div></div><p class="subtle editor-note">${esc(note)}</p><textarea class="editor-text" spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off" wrap="off" aria-label="Contents of ${escValue(path)}"${
    readOnly ? ' readonly' : ''
  }></textarea><div class="editor-status"><span data-editor-pos>Ln 1, Col 1</span><span data-editor-lines>${lines} line${lines === 1 ? '' : 's'}</span><label class="check"><input type="checkbox" data-editor-wrap> Wrap lines</label><span data-editor-state role="status">${readOnly ? 'Read-only' : 'Saved'}</span></div>`;
}

async function call(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { accept: 'application/json', ...authHeaders(), ...(options.headers || {}) } });
  let body = {};
  try {
    body = await response.json();
  } catch {}
  return { ok: response.ok, status: response.status, body };
}

async function waitForHost(requestId) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const result = await operatorRequest(requestId);
    if (!/^(pending|running)$/.test(String(result?.status || ''))) return result;
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  return { status: 'error', message: 'The server did not answer in time; check the History tab.' };
}

// Saves text; returns {ok, sha256?, message}.
export async function saveText({ projectId, area, path, text, sha256 = null }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/files/${area}`;
  const query = new URLSearchParams({ path });
  if (sha256) query.set('expected_sha256', sha256);
  else query.set('on_conflict', 'ask'); // a new file never replaces one
  let requestId = '';
  if (area === 'code') {
    requestId = newRequestId();
    query.set('request_id', requestId);
  }
  const response = await call(`${base}?${query}`, { method: 'PUT', body: new Blob([text], { type: 'text/plain;charset=utf-8' }) });
  if (!response.ok) {
    const message = response.status === 409 && !sha256 ? 'A file with that name already exists.' : errorMessage(response.body, response.status);
    return { ok: false, message };
  }
  if (area === 'code') {
    const result = await waitForHost(requestId);
    if (result?.status !== 'succeeded') {
      const raw = String(result?.message || result?.status || 'failed');
      const message = /changed/i.test(raw) ? 'This file changed on main since you opened it. Copy your text, reopen the file, and apply it again.' : raw;
      return { ok: false, message };
    }
  }
  return { ok: true, sha256: await sha256Of(text), message: area === 'code' ? 'Saved: committed to main' : 'Saved' };
}

async function sha256Of(text) {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null; // plain-http LAN pages: reload the hash from the server instead
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

let element = null;

// Opens the editor. isNew: an empty new file at path. Resolves when it closes
// with true if anything was saved.
export async function openEditor({ projectId, area, path, readOnly = false, isNew = false, areaLabel = '' }) {
  let text = '';
  let sha256 = null;
  if (!isNew) {
    const query = new URLSearchParams({ area, path });
    const response = await call(`/api/projects/${encodeURIComponent(projectId)}/files/text?${query}`);
    if (!response.ok) throw new Error(errorMessage(response.body, response.status));
    ({ text, sha256 } = response.body);
  }
  element?.remove();
  element = document.createElement('dialog');
  element.className = 'laika-dialog file-editor';
  element.innerHTML = editorMarkup({ path, area, text, readOnly, isNew, areaLabel });
  document.body.appendChild(element);
  const box = element.querySelector('.editor-text');
  box.value = text; // not through the markup: HTML drops a leading newline in a textarea
  const state = element.querySelector('[data-editor-state]');
  const pos = element.querySelector('[data-editor-pos]');
  const count = element.querySelector('[data-editor-lines]');
  const indent = indentOf(text);
  let saved = text;
  let savedAny = false;
  let saving = false;
  const dirty = () => !readOnly && box.value !== saved;
  const show = () => {
    const at = caretPosition(box.value, box.selectionStart);
    pos.textContent = `Ln ${at.line}, Col ${at.column}`;
    const lines = box.value.split('\n').length;
    count.textContent = `${lines} line${lines === 1 ? '' : 's'}`;
    if (!saving && !readOnly) state.textContent = dirty() ? 'Unsaved changes' : 'Saved';
  };
  const guard = event => {
    if (dirty()) {
      event.preventDefault();
      event.returnValue = '';
    }
  };
  window.addEventListener('beforeunload', guard);

  const save = async () => {
    if (readOnly || saving || (!dirty() && !isNew)) return;
    saving = true;
    state.textContent = area === 'code' ? 'Committing…' : 'Saving…';
    const value = box.value;
    const result = await saveText({ projectId, area, path, text: value, sha256 });
    saving = false;
    if (!result.ok) {
      state.textContent = result.message;
      return;
    }
    saved = value;
    savedAny = true;
    isNew = false;
    element.querySelector('.pill')?.remove();
    if (result.sha256) sha256 = result.sha256;
    else {
      const again = await call(`/api/projects/${encodeURIComponent(projectId)}/files/text?${new URLSearchParams({ area, path })}`);
      sha256 = again.ok ? again.body.sha256 : null;
    }
    show();
    state.textContent = result.message;
  };

  return new Promise(resolve => {
    const close = () => {
      if (dirty() && !globalThis.confirm?.('Close without saving your changes?')) return;
      window.removeEventListener('beforeunload', guard);
      element.close();
      element.remove();
      element = null;
      resolve(savedAny);
    };
    element.addEventListener('click', event => {
      const action = event.target.closest('[data-editor]')?.dataset.editor;
      if (action === 'save') save();
      if (action === 'close') close();
    });
    element.addEventListener('change', event => {
      if (event.target.matches('[data-editor-wrap]')) box.setAttribute('wrap', event.target.checked ? 'soft' : 'off');
    });
    element.addEventListener('cancel', event => {
      event.preventDefault(); // Esc: ask first if there are changes
      close();
    });
    box.addEventListener('input', show);
    ['keyup', 'click', 'select'].forEach(type => box.addEventListener(type, show));
    box.addEventListener('keydown', event => {
      const mod = event.ctrlKey || event.metaKey;
      if (mod && event.key.toLowerCase() === 's') {
        event.preventDefault();
        save();
      } else if (event.key === 'Tab' && !readOnly && !event.altKey && !mod) {
        // Tab indents (Shift+Tab outdents) instead of leaving the editor; Esc then Tab moves on.
        event.preventDefault();
        const { selectionStart: start, selectionEnd: end, value } = box;
        if (event.shiftKey) {
          const lineStart = value.lastIndexOf('\n', start - 1) + 1;
          const head = value.slice(lineStart, lineStart + indent.length);
          const remove = head === indent ? indent.length : /^[ \t]*/.exec(head)[0].length;
          if (remove) {
            box.setRangeText('', lineStart, lineStart + remove, 'preserve');
            box.selectionStart = box.selectionEnd = Math.max(lineStart, start - remove);
          }
        } else {
          box.setRangeText(indent, start, end, 'end');
        }
        show();
      } else if (event.key === 'Enter' && !readOnly && !mod) {
        // Keep the current line's indentation.
        const { selectionStart: start, selectionEnd: end, value } = box;
        const lineStart = value.lastIndexOf('\n', start - 1) + 1;
        const lead = /^[ \t]*/.exec(value.slice(lineStart, start))[0];
        if (lead) {
          event.preventDefault();
          box.setRangeText(`\n${lead}`, start, end, 'end');
          show();
        }
      }
    });
    element.showModal();
    box.focus();
    box.setSelectionRange(0, 0);
    box.scrollTop = 0;
  });
}
