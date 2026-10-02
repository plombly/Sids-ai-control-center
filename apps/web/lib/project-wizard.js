import { requestJSON, operatorRequest, newRequestId } from './api.js';
import { esc, escValue, pill, text } from './format.js';
import { registerClick, onRoute } from './registry.js';
import { setParent, takePendingParent } from './project-groups.js';

const STEP_LABELS = ['Name', 'Starting point', 'Importance', 'Extras', 'Review'];
const ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/;
const URL_PATTERN = /^(git@[^\s:]+:[^\s]+|ssh:\/\/[^\s]+|https:\/\/[^\s]+)$/;
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

export function slugify(name, existingIds = []) {
  const taken = new Set(existingIds.map(id => String(id).toLowerCase()));
  let base = String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/g, '') || 'project';
  if (base === 'new') base = 'new-2';
  if (!taken.has(base)) return base;
  for (let suffix = 2; ; suffix += 1) {
    const prefix = base.slice(0, 40 - String(suffix).length - 1).replace(/-+$/g, '');
    const candidate = `${prefix}-${suffix}`;
    if (candidate !== 'new' && !taken.has(candidate)) return candidate;
  }
}

export function validateStep(step, state = {}) {
  if (step === 1) {
    if (!String(state.name || '').trim()) return 'Enter a name';
    if (state.id && (!ID_PATTERN.test(state.id) || (state.existingIds || []).includes(state.id))) return 'Choose a valid, available project ID';
  }
  if (step === 2) {
    if (!['empty', 'clone'].includes(state.source)) return 'Choose a starting point';
    if (state.source === 'clone' && !URL_PATTERN.test(String(state.url || '').trim())) return 'Enter the repository address';
  }
  if (step === 3 && !['high', 'medium', 'low'].includes(state.importance)) return 'Choose an importance';
  if (step === 4) {
    if (String(state.push_remote || '').trim() && !URL_PATTERN.test(String(state.push_remote).trim())) return 'Enter a valid push remote';
    if (String(state.gate || '').length > 200 || /[\r\n]/.test(String(state.gate || ''))) return 'Enter a valid test command';
  }
  return '';
}

export function buildCreateBody(state) {
  const body = {
    id: String(state.id || '').trim(),
    name: String(state.name || '').trim(),
    importance: state.importance,
    source: state.source,
    request_id: state.requestId || newRequestId()
  };
  if (state.source === 'clone') body.url = String(state.url || '').trim();
  if (String(state.push_remote || '').trim()) body.push_remote = String(state.push_remote).trim();
  if (String(state.gate || '').trim()) body.gate = String(state.gate).trim();
  return body;
}

export function wizardStepsMarkup(step) {
  return `<div class="wizard-steps">${STEP_LABELS.map((label, index) => {
    const number = index + 1;
    const state = number === step ? ' active' : number < step ? ' done' : '';
    return `<span class="wizard-step${state}">${esc(number)}. ${esc(label)}</span>`;
  }).join('')}</div>`;
}

const field = (label, name, value, options = {}) =>
  `<label class="field">${esc(label)}<input name="${esc(name)}" value="${escValue(value)}"${options.placeholder ? ` placeholder="${esc(options.placeholder)}"` : ''}${options.maxlength ? ` maxlength="${esc(options.maxlength)}"` : ''}></label>${options.hint ? `<span class="field-hint">${options.hint}</span>` : ''}`;

const choice = (type, value, label, hint, selected) =>
  `<button type="button" class="choice${selected ? ' selected' : ''}" data-wizard-choice="${esc(type)}" data-value="${escValue(value)}">${esc(label)}<small>${esc(hint)}</small></button>`;

export function stepMarkup(step, state = {}) {
  if (step === 1) {
    const id = state.id || slugify(state.name, state.existingIds);
    return `${field('What should this project be called?', 'name', state.name || '', { maxlength: 80 })}<span class="field-hint">Project ID: ${esc(id)}</span><button type="button" class="button" data-wizard-id-toggle>Change ID</button>${state.idRevealed ? field('Project ID', 'id', id) : ''}`;
  }
  if (step === 2)
    return `<div class="choice-grid">${choice('source', 'empty', 'Start empty', 'A new, empty repository', state.source === 'empty')}${choice('source', 'clone', 'Import from GitHub', 'Clone an existing repository', state.source === 'clone')}</div>${
      state.source === 'clone'
        ? `${field('Repository address', 'url', state.url || '', { placeholder: 'git@github.com:you/repo.git' })}<span class="field-hint">Use the SSH address from the repository's Code button. Private repositories need a deploy key, which the next screens will show you.</span>`
        : ''
    }`;
  if (step === 3)
    return `${state.parent ? `<p class="subtle">This project will be a child of ${esc(state.parentName || state.parent)}; once it joins, it follows that project's importance.</p>` : ''}<div class="choice-grid">${choice('importance', 'high', 'High', 'Gets workers first', state.importance === 'high')}${choice('importance', 'medium', 'Medium', 'Normal priority', state.importance === 'medium')}${choice('importance', 'low', 'Low', 'Runs when there is spare capacity', state.importance === 'low')}</div>`;
  if (step === 4)
    return `${field('Push merged work to GitHub', 'push_remote', state.push_remote || '')}<span class="field-hint">Leave blank to keep the project local only</span>${field('Test command', 'gate', state.gate || '')}<span class="field-hint">Leave blank and LAIka will detect it (npm test, pytest, cargo test, go test, make test)</span>`;
  const starting = state.source === 'clone' ? `Import from GitHub (${text(state.url)})` : 'Start empty';
  return `<div class="field">${esc(state.name)}<span class="field-hint">Project ID: ${esc(state.id)}</span><span>${esc(starting)}</span><span>${pill(state.importance)}</span><span>Push remote: ${esc(state.push_remote || 'local only')}</span><span>Test command: ${esc(state.gate || 'detect automatically')}</span>${state.parent ? `<span>Part of: ${esc(state.parentName || state.parent)}</span>` : ''}</div>`;
}

export function resultMarkup(result = {}, projectId) {
  const output = typeof result.output === 'string' ? (() => { try { return JSON.parse(result.output); } catch { return {}; } })() : result.output || {};
  const key = output.public_key;
  const pending = output.status === 'pending_key';
  if (result.status === 'succeeded') {
    return `<p class="subtle">Project created${result.groupError ? `, but it could not join the group: ${esc(result.groupError)}` : ''}</p>${key ? `<pre>${esc(key)}</pre><p>${esc('Add this key to the GitHub repository: Settings → Deploy keys → Add deploy key, and tick Allow write access.')}</p><button type="button" class="button" data-wizard-copy-key>Copy key</button>` : ''}${pending ? '<button type="button" class="button" data-wizard-retry-clone>Retry import</button>' : ''}<a class="button" href="#/projects/${esc(encodeURIComponent(projectId))}">Open project</a>`;
  }
  return `<div class="form-status">${esc(result.message || 'Project setup failed')}</div><button type="button" class="button" data-wizard-back>Back</button>`;
}

let active = false;
let state;
let root;

const render = () => {
  if (!root || !state) return;
  const error = validateStep(state.step, state);
  const focused = typeof document !== 'undefined' ? document.activeElement : null;
  const focusName = focused && root.contains(focused) ? focused.name : '';
  const selection = focusName ? [focused.selectionStart, focused.selectionEnd] : [];
  root.innerHTML = `<section class="panel"><p class="eyebrow">CREATE PROJECT</p><div class="wizard">${wizardStepsMarkup(state.step)}<div>${stepMarkup(state.step, state)}</div>${state.error || error ? `<div class="form-status">${esc(state.error || error)}</div>` : ''}<div class="wizard-actions"><a class="button" href="#/projects"${state.step === 1 ? '' : ' hidden'}>Cancel</a><button type="button" class="button" data-wizard-back${state.step === 1 || state.busy ? ' hidden' : ''}>Back</button><button type="button" class="button" data-wizard-next${state.busy || !!error ? ' disabled' : ''}>${state.step === 5 ? 'Create project' : 'Next'}</button></div></div></section>`;
  if (focusName) {
    const next = root.querySelector(`input[name="${focusName}"]`);
    if (next) {
      next.focus();
      if (selection[0] != null) next.setSelectionRange(selection[0], selection[1]);
    }
  }
};

const updateFromInput = input => {
  if (!state) return;
  state[input.name] = input.value;
  if (input.name === 'name' && !state.idRevealed) state.id = slugify(input.value, state.existingIds);
  if (state.source === 'clone' && input.name === 'url' && !state.pushTouched && /^git@/.test(input.value)) state.push_remote = input.value;
  if (input.name === 'push_remote') state.pushTouched = true;
  state.error = '';
  render();
};

const poll = async requestId => {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const result = await operatorRequest(requestId);
    if (['succeeded', 'refused', 'error', 'expired', 'interrupted'].includes(result.status)) return result;
    await sleep(2000);
  }
  return { status: 'expired', message: 'Project setup timed out' };
};

const renderResult = () => {
  if (root && state?.result) root.innerHTML = `<section class="panel"><p class="eyebrow">CREATE PROJECT</p><div class="wizard">${resultMarkup(state.result, state.id)}</div></section>`;
};

const create = async () => {
  state.busy = true;
  state.error = 'Setting up your project…';
  render();
  try {
    const response = await requestJSON('/api/projects', { method: 'POST', body: JSON.stringify(buildCreateBody(state)) });
    state.requestId = response.request_id || state.requestId;
    state.result = await poll(state.requestId);
    if (state.result.status === 'succeeded' && state.parent) {
      try {
        await setParent(state.id, state.parent);
      } catch (error) {
        state.result = { ...state.result, groupError: error.message };
      }
    }
    state.busy = false;
    state.error = '';
    renderResult();
  } catch (error) {
    state.busy = false;
    state.error = error.message;
    render();
  }
};

const retry = async () => {
  state.busy = true;
  state.error = 'Setting up your project…';
  render();
  try {
    const response = await requestJSON(`/api/projects/${encodeURIComponent(state.id)}/retry-clone`, { method: 'POST', body: JSON.stringify({ request_id: newRequestId() }) });
    state.result = await poll(response.request_id);
  } catch (error) {
    state.result = { status: 'error', message: error.message };
  }
  state.busy = false;
  renderResult();
};

registerClick('wizardChoice', button => {
  state[button.dataset.wizardChoice] = button.dataset.value;
  if (button.dataset.wizardChoice === 'source' && button.dataset.value === 'clone' && !state.pushTouched && /^git@/.test(state.url || '')) state.push_remote = state.url;
  state.error = '';
  render();
});
registerClick('wizardIdToggle', () => { state.idRevealed = true; render(); });
registerClick('wizardNext', () => {
  const error = validateStep(state.step, state);
  if (error) { state.error = error; render(); return; }
  if (state.step === 5) create();
  else { state.step += 1; state.error = ''; render(); }
});
registerClick('wizardBack', () => {
  if (state.result) {
    state.result = null;
    state.requestId = newRequestId();
    state.step = 5;
    render();
    return;
  }
  if (state.step > 1) { state.step -= 1; state.error = ''; render(); } });
registerClick('wizardCopyKey', button => {
  const key = button.parentElement?.querySelector('pre')?.textContent;
  globalThis.navigator?.clipboard?.writeText?.(key)?.catch?.(() => {});
});
registerClick('wizardRetryClone', retry);

if (typeof document !== 'undefined') {
  document.addEventListener('input', event => {
    if (event.target?.closest('.wizard')) updateFromInput(event.target);
  });
}

onRoute(async route => {
  const entering = route.view === 'projects' && route.create === true;
  if (!entering) { active = false; return; }
  if (active) return;
  active = true;
  state = { step: 1, importance: globalThis.LAIKA_PREFS?.DEFAULT_IMPORTANCE || 'medium', source: '', existingIds: [], requestId: newRequestId(), parent: takePendingParent() };
  if (typeof document === 'undefined') return;
  root = document.getElementById('projects-root');
  if (!root) return;
  render();
  try {
    const projects = await requestJSON('/api/projects');
    state.existingIds = Array.isArray(projects) ? projects.map(project => project.id) : [];
    state.parentName = (Array.isArray(projects) ? projects : []).find(project => project.id === state.parent)?.name || '';
    state.id = slugify(state.name, state.existingIds);
    render();
  } catch (error) {
    state.error = error.message;
    render();
  }
});
