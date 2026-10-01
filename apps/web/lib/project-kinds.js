// What a project is (detected type, or the owner's choice / description),
// goal templates for that type, and its Builds tab. Markup is pure; the
// handlers at the bottom ask lib/projects.js to redraw through a
// 'sid:project-refresh' event.
import { requestJSON } from './api.js';
import { esc, escValue, pill, text } from './format.js';
import { registerClick } from './registry.js';
import { useTemplate } from './goal-assistant.js';

let catalogCache = null;
export async function loadCatalog(fetchJSON = requestJSON) {
  if (!catalogCache) catalogCache = fetchJSON('/api/project-catalog').catch(error => {
    catalogCache = null;
    throw error;
  });
  return catalogCache;
}

// esc() shows '—' for empty values; these places want nothing instead.
const opt = value => (value || value === 0 ? esc(value) : '');
const typeOf = (catalog, key) => catalog?.types?.[key] || null;
const BUILT = /^(game|desktop_app|mobile_cross|android_app|cli_tool|library|embedded_iot|browser_extension|networking_tool)$/;

export function sizeText(bytes) {
  const value = Number(bytes) || 0;
  return value >= 1e9 ? `${(value / 1e9).toFixed(1)} GB` : value >= 1e6 ? `${(value / 1e6).toFixed(1)} MB` : value >= 1e3 ? `${Math.round(value / 1e3)} KB` : `${value} B`;
}

export function agoText(seconds, now = Date.now() / 1000) {
  const age = now - (Number(seconds) || 0);
  if (!seconds || age < 0) return '';
  if (age < 90) return 'just now';
  if (age < 5400) return `${Math.round(age / 60)} min ago`;
  if (age < 129600) return `${Math.round(age / 3600)} h ago`;
  return `${Math.round(age / 86400)} days ago`;
}

// The card at the top of Overview: type, why SID thinks so, templates and
// the type's main action (games and apps get Build, services get their app).
export function kindCardMarkup(project, catalog) {
  const id = text(project?.id, '');
  const kind = project?.project_type || {};
  const key = text(kind.type, 'checking');
  const info = typeOf(catalog, key);
  const recipe = catalog?.recipes?.[kind.stack] || null;
  const stack = recipe ? ` · ${esc(recipe.label)}` : '';
  const source = { chosen: 'set by you', described: 'from your description', detected: 'detected' }[kind.source] || '';
  let head;
  if (key === 'checking') head = '<span class="kind-icon" aria-hidden="true">⏳</span><span><b>Looking at the project…</b></span>';
  else if (!info) {
    head = `<span class="kind-icon" aria-hidden="true">❔</span><span><b>Not recognised yet</b><span class="subtle"> SID checks again every hour and whenever main changes.</span></span>`;
  } else head = `<span class="kind-icon" aria-hidden="true">${esc(info.icon)}</span><span><b>${esc(info.label)}</b>${stack}${source ? `<span class="subtle"> · ${esc(source)}</span>` : ''}</span>`;
  const evidence = (kind.evidence || []).length
    ? `<details class="kind-why"><summary>Why?</summary><ul>${kind.evidence.map(item => `<li>${esc(item)}</li>`).join('')}</ul></details>`
    : '';
  const unknownHelp = !info && key !== 'checking' && id !== 'sid'
    ? `<p class="subtle">Tell SID what it is: <a href="#/projects/${encodeURIComponent(id)}/settings">describe it or pick a type in Settings</a>.</p>`
    : '';
  const canBuild = id !== 'sid' && recipe && !recipe.unsupported && (BUILT.test(key) || !info?.runs);
  const action = canBuild
    ? `<button type="button" class="primary" data-build-start="${esc(id)}">Build</button><a class="button" href="#/projects/${encodeURIComponent(id)}/builds">Builds</a>`
    : '';
  const recheck = id !== 'sid' ? `<button type="button" class="detail-button" data-kind-recheck="${esc(id)}">Recheck</button>` : '';
  const templates = (info || typeOf(catalog, 'other'))?.templates || [];
  const chips = templates.length && project?.status !== 'archived'
    ? `<div class="template-chips" role="group" aria-label="Goal ideas">${templates
        .map(item => `<button type="button" class="template-chip" data-goal-template="${escValue(item.text)}">${esc(item.title)}</button>`)
        .join('')}</div>`
    : '';
  return `<div class="kind-card"><div class="kind-head">${head}<span class="kind-actions">${action}${recheck}</span></div>${evidence}${unknownHelp}${chips}<span id="kind-status" class="form-status" role="status"></span></div>`;
}

// Settings: type, description and build overrides.
export function kindSettingsMarkup(project, catalog) {
  const id = text(project?.id, '');
  const kind = project?.project_type || {};
  const detected = typeOf(catalog, kind.detected);
  const options = Object.entries(catalog?.types || {})
    .map(([key, value]) => `<option value="${escValue(key)}"${kind.chosen === key ? ' selected' : ''}>${esc(value.icon)} ${esc(value.label)}</option>`)
    .join('');
  const auto = `Automatic (${detected ? esc(detected.label) : 'not recognised'})`;
  const buildFields = id === 'sid' ? '' : `<h4>Build</h4><p class="subtle">Empty fields use the recipe for the detected stack. The command runs in a Docker container that only sees a copy of main.</p><label class="field">Build command<input name="build_command" value="${escValue(project.build_command)}" placeholder="From the detected stack" autocomplete="off"></label><label class="field">Docker image<input name="build_image" value="${escValue(project.build_image)}" placeholder="e.g. node:22-bookworm" autocomplete="off"></label><label class="field">Output folder<input name="build_output" value="${escValue(project.build_output)}" placeholder="e.g. dist" autocomplete="off"></label>`;
  return `<form id="project-type-form" class="goal-form kind-settings"><h3>Project type</h3><label class="field">Type<select name="type"><option value="">${auto}</option>${options}</select></label><label class="field">Describe it<textarea name="type_description" rows="2" maxlength="500" placeholder="e.g. a Discord bot that posts server stats, or a 2D platformer in Godot">${opt(kind.description)}</textarea></label><span class="field-hint">Used when SID cannot tell what the project is, and to pick goal ideas.</span>${buildFields}<div class="form-row"><button type="submit">Save</button>${id === 'sid' ? '' : `<button type="button" data-kind-recheck="${esc(id)}">Recheck now</button>`}<span id="project-type-status" class="form-status" role="status"></span></div></form>`;
}

export function kindRequest(values) {
  const body = { type: text(values.type, '').trim(), type_description: text(values.type_description, '').replace(/\s+/g, ' ').trim() };
  for (const key of ['build_command', 'build_image', 'build_output']) if (key in values) body[key] = text(values[key], '').trim();
  return body;
}

const BUILD_STATES = { queued: 'Waiting to start', running: 'Building…', succeeded: 'Ready', failed: 'Failed' };

export function buildsMarkup(id, data, now = Date.now() / 1000) {
  const recipe = data?.recipe || {};
  const builds = Array.isArray(data?.builds) ? data.builds : [];
  const pending = data?.requested || builds.some(build => /^(queued|running)$/.test(build.status));
  const what = recipe.unsupported
    ? `<div class="notice">${esc(recipe.unsupported)}</div>`
    : `<p class="subtle">${esc(recipe.label || 'Build')}${recipe.command ? `: <code>${esc(recipe.command)}</code>` : ''}${recipe.image ? ` in <code>${esc(recipe.image)}</code>` : ''}</p>${
        recipe.needs ? `<p class="subtle">Needs ${esc(recipe.needs)}.</p>` : ''
      }${recipe.note ? `<p class="subtle">${esc(recipe.note)}</p>` : ''}`;
  const button = recipe.unsupported
    ? ''
    : `<button type="button" class="primary" data-build-start="${esc(id)}"${pending ? ' disabled' : ''}>${pending ? 'Building…' : 'Build now'}</button>`;
  const rows = builds
    .map(build => {
      const bid = encodeURIComponent(build.id);
      const base = `/api/projects/${encodeURIComponent(id)}/builds/${bid}`;
      const took = build.finished_at && build.started_at ? ` · ${Math.max(1, Math.round(build.finished_at - build.started_at))} s` : '';
      const download = build.status === 'succeeded' ? `<a class="button" href="${escValue(base)}/download" download>Download (${esc(sizeText(build.size))})</a>` : '';
      const log = build.status !== 'queued' ? `<a class="button" href="${escValue(base)}/log" target="_blank" rel="noopener">Log</a>` : '';
      const error = build.error ? `<div class="form-status">${esc(build.error)}</div>` : '';
      return `<div class="item build-item"><div class="item-head"><span class="item-title">${pill(build.status)} ${opt(BUILD_STATES[build.status])}</span><span class="subtle">${opt(agoText(build.requested_at || build.started_at, now))}${build.commit ? ` · main ${esc(build.commit.slice(0, 8))}` : ''}${opt(took)}</span></div><div class="form-row">${download}${log}</div>${error}</div>`;
    })
    .join('');
  return `<div class="builds"><div class="item-head"><h3>Builds</h3>${button}</div>${what}<span id="build-status" class="form-status" role="status"></span><div class="stack">${rows || '<div class="empty">No builds yet</div>'}</div><p class="subtle">The newest 5 builds are kept.</p></div>`;
}

if (typeof document !== 'undefined') {
  const refresh = () => window.dispatchEvent(new CustomEvent('sid:project-refresh'));
  const say = message => {
    for (const id of ['build-status', 'kind-status', 'project-type-status']) {
      const node = document.getElementById(id);
      if (node) node.textContent = message;
    }
  };
  registerClick('goalTemplate', button => {
    const id = decodeURIComponent(location.hash.split('/')[2] || '');
    if (id) useTemplate(`project:${id}`, button.dataset.goalTemplate);
  });
  registerClick('buildStart', async button => {
    button.disabled = true;
    try {
      await requestJSON(`/api/projects/${encodeURIComponent(button.dataset.buildStart)}/builds`, { method: 'POST' });
      if (!location.hash.endsWith('/builds')) location.hash = `#/projects/${encodeURIComponent(button.dataset.buildStart)}/builds`;
      else refresh();
    } catch (error) {
      button.disabled = false;
      say(error.message);
    }
  });
  registerClick('kindRecheck', async button => {
    button.disabled = true;
    try {
      await requestJSON(`/api/projects/${encodeURIComponent(button.dataset.kindRecheck)}/recheck`, { method: 'POST' });
      say('Checking… this takes a few seconds');
      setTimeout(refresh, 6000);
    } catch (error) {
      button.disabled = false;
      say(error.message);
    }
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (form?.id !== 'project-type-form') return;
    event.preventDefault();
    const id = location.hash.split('/')[2];
    try {
      await requestJSON(`/api/projects/${id}`, { method: 'PATCH', body: JSON.stringify(kindRequest(Object.fromEntries(new FormData(form).entries()))) });
      document.activeElement?.blur?.();
      say('Saved');
      refresh();
    } catch (error) {
      say(error.message);
    }
  });
}
