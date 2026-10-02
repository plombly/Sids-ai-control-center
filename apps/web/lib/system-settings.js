// Settings pages built from the server's settings schema
// (apps/api/settings_schema.py via GET /api/settings). One generic form per
// section; saving validates on the server, and settings that need services
// restarted are collected under "Waiting to be applied" with an Apply button.
import { requestJSON, newRequestId } from './api.js';
import { esc, escValue } from './format.js';

export const EXTRA_SECTIONS = [
  { id: 'notifications', label: 'Notifications', help: 'Where alerts go and what each event does.' },
  { id: 'phones', label: 'Phones & apps', help: 'Pair the LAIka app and revoke lost phones.' },
  { id: 'system', label: 'System', help: 'Version, branding and where things live.' }
];

export function navMarkup(schema, current) {
  const sections = [...(schema?.sections || []).slice(0, 2), ...EXTRA_SECTIONS.slice(0, 1), ...(schema?.sections || []).slice(2), ...EXTRA_SECTIONS.slice(1)];
  return `<nav class="settings-nav" aria-label="Settings sections">${sections
    .map(section => `<a href="#/settings/${escValue(section.id)}"${section.id === current ? ' class="active" aria-current="page"' : ''}>${esc(section.label)}</a>`)
    .join('')}</nav>`;
}

const APPLY_NOTES = { live: '', restart: 'Applies when services restart', host: 'Changes this server when applied' };

export function fieldMarkup(field, value) {
  const id = `set-${field.key}`;
  const name = escValue(field.key);
  const note = APPLY_NOTES[field.apply] ? `<span class="setting-apply">${esc(APPLY_NOTES[field.apply])}</span>` : '';
  const help = field.help ? `<span class="field-hint">${esc(field.help)}</span>` : '';
  const label = (choice) => esc(field.choice_labels?.[choice] || choice);
  let control;
  if (field.type === 'bool') {
    control = `<label class="switch"><input type="checkbox" id="${id}" name="${name}"${value === 'true' ? ' checked' : ''}> <span>${value === 'true' ? 'On' : 'Off'}</span></label>`;
  } else if (field.type === 'choice') {
    control = `<select id="${id}" name="${name}">${field.choices.map(choice => `<option value="${escValue(choice)}"${choice === value ? ' selected' : ''}>${label(choice)}</option>`).join('')}</select>`;
  } else if (field.type === 'list') {
    const chosen = String(value || '').split(',').filter(Boolean);
    const ordered = [...chosen, ...field.choices.filter(choice => !chosen.includes(choice))];
    control = `<div class="list-setting" id="${id}" data-list="${name}">${ordered
      .map(choice => `<label class="list-item"><input type="checkbox" value="${escValue(choice)}"${chosen.includes(choice) ? ' checked' : ''}> ${label(choice)}<span class="list-move"><button type="button" class="detail-button" data-list-up="${escValue(choice)}" aria-label="Move up">↑</button><button type="button" class="detail-button" data-list-down="${escValue(choice)}" aria-label="Move down">↓</button></span></label>`)
      .join('')}</div>`;
  } else if (field.type === 'color') {
    control = `<div class="color-setting"><input type="color" id="${id}" name="${name}" value="${escValue(value)}">${(field.presets || [])
      .map(color => `<button type="button" class="swatch${color === value ? ' chosen' : ''}" style="background:${escValue(color)}" data-swatch="${escValue(color)}" data-for="${id}" aria-label="${escValue(color)}"></button>`)
      .join('')}</div>`;
  } else {
    const type = field.type === 'int' || field.type === 'float' ? 'number' : field.type === 'time' ? 'time' : 'text';
    const step = field.type === 'float' ? ' step="any"' : '';
    const range = field.min !== undefined ? ` min="${escValue(field.min)}" max="${escValue(field.max)}"` : '';
    control = `<input type="${type}" id="${id}" name="${name}" value="${escValue(value)}"${step}${range} autocomplete="off">`;
  }
  return `<div class="setting-row" data-key="${name}"><label class="setting-label" for="${id}">${esc(field.label)}</label><div class="setting-control">${control}${help}${note}<span class="form-status setting-error" role="status"></span></div></div>`;
}

export function sectionMarkup(schema, sectionId, values = {}, pending = []) {
  const section = (schema?.sections || []).find(item => item.id === sectionId);
  if (!section) return '';
  const fields = (schema.fields || []).filter(field => field.section === sectionId);
  return `<form class="settings-card settings-form" data-section="${escValue(sectionId)}"><h3>${esc(section.label)}</h3><p class="subtle">${esc(section.help)}</p>${fields
    .map(field => fieldMarkup(field, values[field.key] ?? field.default))
    .join('')}<div class="settings-actions"><button type="submit" class="primary">Save</button><span class="form-status" role="status"></span></div></form>${pendingMarkup(schema, pending)}`;
}

export function pendingMarkup(schema, pending = []) {
  if (!pending.length) return '';
  const labels = pending.map(key => (schema.fields || []).find(field => field.key === key)?.label || key);
  return `<div class="settings-card pending-card"><h3>Waiting to be applied</h3><p class="subtle">${esc(labels.join(', '))}</p><p class="subtle">Apply restarts LAIka's services. Running jobs finish first, so it can take a few minutes; nothing is lost.</p><div class="settings-actions"><button type="button" class="primary" data-settings-apply>Apply now</button><span class="form-status" id="settings-apply-status" role="status"></span></div></div>`;
}

// Values from a section form, only the ones that changed.
export function changedValues(form, values = {}) {
  const changes = {};
  form.querySelectorAll('.setting-row').forEach(row => {
    const key = row.dataset.key;
    const list = row.querySelector('[data-list]');
    let value;
    if (list) value = [...list.querySelectorAll('input[type="checkbox"]')].filter(box => box.checked).map(box => box.value).join(',');
    else {
      const input = row.querySelector('input, select');
      value = input.type === 'checkbox' ? (input.checked ? 'true' : 'false') : input.value;
    }
    if (String(value) !== String(values[key] ?? '')) changes[key] = value;
  });
  return changes;
}

export function systemMarkup(info = {}) {
  return `<div class="settings-card"><h3>System</h3><div class="setting-facts"><span>Version</span><b>${esc(info.version || 'development')}</b><span>Commit</span><b>${esc(info.commit || '—')}</b><span>Server name</span><b>${esc(info.server_name || 'LAIka')}</b></div></div><div class="settings-card"><h3>Name and logo</h3><p class="subtle">LAIka's name and logo are part of the installation, not a setting. To change them, edit <code>/etc/laika/branding.json</code> and the logo files on the server, then run <code>sudo laika branding apply</code>.</p></div>`;
}

let schemaCache = null;
export async function loadSettings(force = false) {
  if (!schemaCache || force) schemaCache = await requestJSON('/api/settings');
  return schemaCache;
}

if (typeof document !== 'undefined') {
  const statusIn = (node, message) => {
    const target = node?.querySelector?.('.settings-actions .form-status') || node;
    if (target) target.textContent = message;
  };
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form?.classList?.contains('settings-form')) return;
    event.preventDefault();
    const data = await loadSettings();
    const changes = changedValues(form, data.values);
    form.querySelectorAll('.setting-error').forEach(node => (node.textContent = ''));
    if (!Object.keys(changes).length) return statusIn(form, 'Nothing changed');
    try {
      schemaCache = await requestJSON('/api/settings', { method: 'PUT', body: JSON.stringify({ changes }) });
      window.dispatchEvent(new CustomEvent('laika:settings-changed', { detail: schemaCache.values }));
      window.dispatchEvent(new CustomEvent('laika:settings-redraw'));
    } catch (error) {
      const errors = error.body?.detail?.errors;
      if (errors) {
        for (const [key, message] of Object.entries(errors)) {
          const node = form.querySelector(`.setting-row[data-key="${CSS.escape(key)}"] .setting-error`);
          if (node) node.textContent = message;
        }
        statusIn(form, 'Some values need fixing');
      } else statusIn(form, error.message);
    }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.swatch) {
      const input = document.getElementById(button.dataset.for);
      if (input) input.value = button.dataset.swatch;
      button.parentElement.querySelectorAll('.swatch').forEach(node => node.classList.toggle('chosen', node === button));
    } else if (button.dataset.listUp || button.dataset.listDown) {
      const item = button.closest('.list-item');
      if (button.dataset.listUp && item.previousElementSibling) item.parentElement.insertBefore(item, item.previousElementSibling);
      if (button.dataset.listDown && item.nextElementSibling) item.parentElement.insertBefore(item.nextElementSibling, item);
    } else if (button.dataset.settingsApply !== undefined) {
      button.disabled = true;
      const status = document.getElementById('settings-apply-status');
      try {
        const result = await requestJSON('/api/settings/apply', { method: 'POST', body: JSON.stringify({ what: 'apply', request_id: newRequestId() }) });
        if (status) status.textContent = result.message || 'Applying. Services restart once running jobs finish.';
        await loadSettings(true);
      } catch (error) {
        button.disabled = false;
        if (status) status.textContent = error.message;
      }
    }
  });
  document.addEventListener('change', event => {
    const box = event.target;
    if (box?.closest?.('.switch')) box.nextElementSibling.textContent = box.checked ? 'On' : 'Off';
  });
}
