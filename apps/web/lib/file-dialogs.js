// In-page dialogs for the file browser (native <dialog>, styled like the
// dashboard) instead of the browser's prompt()/confirm() boxes.
import { esc, escValue } from './format.js';
import { fileKind, kindIcon } from './file-kinds.js';

export const CONFLICT_CHOICES = [
  ['keep', 'Keep both'],
  ['overwrite', 'Overwrite'],
  ['skip', 'Discard']
];

export function conflictDialogMarkup(names, area) {
  const warning =
    area === 'data'
      ? 'Overwriting app data is permanent.'
      : 'Overwritten code stays in the git history of main.';
  const options = (selected = 'keep') =>
    CONFLICT_CHOICES.map(([value, label]) => `<option value="${value}"${value === selected ? ' selected' : ''}>${esc(label)}</option>`).join('');
  const rows = names
    .map((name, index) => `<div class="conflict-row"><span>${esc(name)}</span><select data-conflict="${index}" aria-label="What to do with ${escValue(name)}">${options()}</select></div>`)
    .join('');
  const all = names.length > 1
    ? `<label class="conflict-all">Apply to all <select data-conflict-all aria-label="Apply to all"><option value="">Choose per item</option>${options('')}</select></label>`
    : '';
  return `<h3>${names.length === 1 ? 'This name already exists' : `${names.length} names already exist`}</h3><p class="subtle">Keep both adds a number, e.g. "name (2)". Discard leaves the existing item and skips the new one. ${esc(warning)}</p>${all}<div class="conflict-list">${rows}</div>`;
}

export function folderPickerMarkup({ area, path, folders, areas, labels }) {
  const tabs = areas.length > 1
    ? `<div class="file-tabs">${areas.map(key => `<button type="button" class="${key === area ? 'active' : ''}" data-pick-area="${key}">${esc(labels[key])}</button>`).join('')}</div>`
    : '';
  const crumbs = [`<button type="button" class="detail-button" data-pick-path="">${esc(labels[area])}</button>`];
  const parts = path ? path.split('/') : [];
  parts.forEach((part, index) => crumbs.push(`<button type="button" class="detail-button" data-pick-path="${escValue(parts.slice(0, index + 1).join('/'))}">${esc(part)}</button>`));
  const list = folders === null
    ? '<div class="empty">Loading…</div>'
    : folders.length
      ? folders.map(name => `<button type="button" class="picker-folder" data-pick-path="${escValue(path ? `${path}/${name}` : name)}">${kindIcon('folder')}<span>${esc(name)}</span></button>`).join('')
      : '<div class="empty">No folders here</div>';
  return `${tabs}<div class="breadcrumb">${crumbs.join('<span class="subtle"> / </span>')}</div><div class="picker-list">${list}</div>`;
}

let dialogElement = null;

function dialog() {
  if (dialogElement && document.body.contains(dialogElement)) return dialogElement;
  dialogElement = document.createElement('dialog');
  dialogElement.className = 'laika-dialog';
  document.body.appendChild(dialogElement);
  return dialogElement;
}

// Shows body + buttons; resolves with the clicked button's value (or null
// on Esc). onReady(element) can wire inputs; collect(element) reads them.
function open(bodyHtml, buttons, { onReady, collect } = {}) {
  const element = dialog();
  const actions = buttons
    .map(([value, label, kind]) => `<button type="button" data-dialog-value="${escValue(value)}" class="${kind || ''}">${esc(label)}</button>`)
    .join('');
  element.innerHTML = `<div class="laika-dialog-body">${bodyHtml}</div><div class="laika-dialog-actions">${actions}</div>`;
  return new Promise(resolve => {
    let done = false;
    const finish = value => {
      if (done) return;
      done = true;
      const result = value === null ? null : collect ? collect(element, value) : value;
      element.removeEventListener('click', onClick);
      element.removeEventListener('cancel', onCancel);
      if (element.open) element.close();
      resolve(result);
    };
    const onClick = event => {
      const button = event.target.closest('[data-dialog-value]');
      if (button) finish(button.dataset.dialogValue === '__cancel' ? null : button.dataset.dialogValue);
    };
    const onCancel = event => {
      event.preventDefault();
      finish(null);
    };
    element.addEventListener('click', onClick);
    element.addEventListener('cancel', onCancel);
    element.showModal();
    onReady?.(element, finish);
  });
}

export function askText({ title, label, value = '', confirm = 'OK', selectStem = false }) {
  const body = `<h3>${esc(title)}</h3><label class="field">${esc(label)}<input data-dialog-input value="${escValue(value)}" autocomplete="off"></label>`;
  return open(body, [['__cancel', 'Cancel'], ['ok', confirm]], {
    onReady: (element, finish) => {
      const input = element.querySelector('[data-dialog-input]');
      input.focus();
      const dot = value.lastIndexOf('.');
      if (selectStem && dot > 0) input.setSelectionRange(0, dot);
      else input.select();
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          finish('ok');
        }
      });
    },
    collect: element => element.querySelector('[data-dialog-input]').value.trim() || null
  });
}

export function askConfirm({ title, message, items = [], confirm = 'OK', danger = false }) {
  const shown = items.slice(0, 12).map(item => `<li>${esc(item)}</li>`).join('');
  const more = items.length > 12 ? `<li class="subtle">…and ${items.length - 12} more</li>` : '';
  const body = `<h3>${esc(title)}</h3><p>${esc(message)}</p>${items.length ? `<ul class="dialog-items">${shown}${more}</ul>` : ''}`;
  return open(body, [['__cancel', 'Cancel'], ['ok', confirm, danger ? 'danger-button' : '']]).then(value => value === 'ok');
}

// Resolves {resolutions: {name: choice}} or null when cancelled.
export function askConflicts(names, area) {
  return open(conflictDialogMarkup(names, area), [['__cancel', 'Cancel'], ['ok', 'Continue']], {
    onReady: element => {
      element.querySelector('[data-conflict-all]')?.addEventListener('change', event => {
        if (!event.target.value) return;
        element.querySelectorAll('[data-conflict]').forEach(select => {
          select.value = event.target.value;
        });
      });
    },
    collect: element => {
      const resolutions = {};
      element.querySelectorAll('[data-conflict]').forEach(select => {
        resolutions[names[Number(select.dataset.conflict)]] = select.value;
      });
      return { resolutions };
    }
  });
}

// Browse folders (listFolders(area, path) -> [names]) and pick one.
// Resolves {area, path} or null.
export function askFolder({ title, area, path = '', areas = [area], labels, listFolders, confirm = 'Choose this folder' }) {
  let current = { area, path };
  let folders = null;
  const body = () => `<h3>${esc(title)}</h3><div data-picker>${folderPickerMarkup({ ...current, folders, areas, labels })}</div>`;
  return open(body(), [['__cancel', 'Cancel'], ['ok', confirm]], {
    onReady: element => {
      const refresh = async () => {
        folders = null;
        element.querySelector('[data-picker]').innerHTML = folderPickerMarkup({ ...current, folders, areas, labels });
        try {
          folders = await listFolders(current.area, current.path);
        } catch {
          folders = [];
        }
        element.querySelector('[data-picker]').innerHTML = folderPickerMarkup({ ...current, folders, areas, labels });
      };
      element.addEventListener('click', event => {
        const go = event.target.closest('[data-pick-path]');
        const tab = event.target.closest('[data-pick-area]');
        if (go) current = { ...current, path: go.dataset.pickPath };
        else if (tab) current = { area: tab.dataset.pickArea, path: '' };
        else return;
        refresh();
      });
      refresh();
    },
    collect: () => current
  });
}

// For tests and the listing: one icon + tinted name for an entry.
export function entryBadge(entry) {
  const { kind, label } = fileKind(entry);
  return `<span class="file-name kind-${kind}" title="${escValue(label)}">${kindIcon(kind)}<span>${esc(entry.name)}</span></span>`;
}
