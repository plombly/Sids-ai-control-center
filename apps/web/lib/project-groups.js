// Project groups: a parent project controls its children (one level). The
// API marks each project with parent / parent_name / children / managed;
// this module draws the group parts of a project page and handles
// attach, detach and "new child project".
import { requestJSON } from './api.js';
import { esc, escValue } from './format.js';
import { registerClick } from './registry.js';

const link = id => `#/projects/${encodeURIComponent(id)}`;

// "Part of Shop" under a child's name.
export function partOfMarkup(project) {
  return project?.parent ? `<p class="part-of">Part of <a href="${escValue(link(project.parent))}">${esc(project.parent_name || project.parent)}</a></p>` : '';
}

// Parents first, each followed by its children, for lists that nest them.
export function nestProjects(projects = []) {
  const ids = new Set(projects.map(project => project.id));
  const top = projects.filter(project => !project.parent || !ids.has(project.parent));
  return top.map(project => ({ ...project, members: projects.filter(child => child.parent === project.id) }));
}

// Overview of a parent (or a project that could become one).
export function groupOverviewMarkup(project) {
  const id = project?.id;
  if (!id || id === 'sid' || project.parent) return '';
  const children = Array.isArray(project.children) ? project.children : [];
  const rows = children
    .map(child => `<li><a href="${escValue(link(child.id))}">${esc(child.name || child.id)}</a>${child.status && child.status !== 'active' ? ` <span class="subtle">${esc(child.status)}</span>` : ''}</li>`)
    .join('');
  const intro = children.length
    ? 'Goals given here can plan work in any of these projects. They follow this project\'s importance and internet settings.'
    : 'Add child projects (for example an app for this service) to plan work across them from here.';
  return `<div class="group-card"><div class="item-head"><h3>${children.length ? 'Child projects' : 'Project group'}</h3><button type="button" data-group-new-child="${escValue(id)}">New child project</button></div><p class="subtle">${esc(intro)}</p>${rows ? `<ul class="group-children">${rows}</ul>` : ''}</div>`;
}

// Settings: which group the project belongs to.
export function groupSettingsMarkup(project, projects = []) {
  const id = project?.id;
  if (!id || id === 'sid') return '';
  const children = Array.isArray(project.children) ? project.children : [];
  if (children.length) {
    const rows = children
      .map(child => `<li><a href="${escValue(link(child.id))}">${esc(child.name || child.id)}</a><button type="button" class="detail-button" data-group-detach="${escValue(child.id)}">Remove from group</button></li>`)
      .join('');
    return `<div class="goal-form group-settings"><h3>Project group</h3><p class="subtle">This project is the parent of:</p><ul class="group-children">${rows}</ul><p class="subtle">A parent cannot also be a child project.</p><span id="group-status" class="form-status" role="status"></span></div>`;
  }
  const eligible = projects.filter(other => other.id !== id && other.id !== 'sid' && !other.parent && other.status === 'active');
  const options = eligible.map(other => `<option value="${escValue(other.id)}"${project.parent === other.id ? ' selected' : ''}>${esc(other.name || other.id)}</option>`).join('');
  return `<form id="project-group-form" class="goal-form group-settings"><h3>Project group</h3><label class="field">Part of<select name="parent"><option value="">No group (a normal project)</option>${options}</select></label><span class="field-hint">A child project follows its parent's importance and internet settings, and the parent's goals can plan work in it. It keeps its own code, tests, builds and approvals.</span><div class="form-row"><button type="submit">Save</button><span id="group-status" class="form-status" role="status"></span></div></form>`;
}

const PENDING = 'sid-new-child-of';
export function takePendingParent() {
  try {
    const value = sessionStorage.getItem(PENDING) || '';
    sessionStorage.removeItem(PENDING);
    return value;
  } catch {
    return '';
  }
}

export async function setParent(id, parent) {
  return requestJSON(`/api/projects/${encodeURIComponent(id)}/parent`, { method: 'POST', body: JSON.stringify({ parent }) });
}

if (typeof document !== 'undefined') {
  const refresh = () => window.dispatchEvent(new CustomEvent('sid:project-refresh'));
  const say = message => {
    const node = document.getElementById('group-status');
    if (node) node.textContent = message;
  };
  registerClick('groupNewChild', button => {
    try {
      sessionStorage.setItem(PENDING, button.dataset.groupNewChild);
    } catch {}
    location.hash = '#/projects/new';
  });
  registerClick('groupDetach', async button => {
    if (!globalThis.confirm?.(`Remove ${button.dataset.groupDetach} from this group? It becomes a normal project again; nothing is deleted.`)) return;
    button.disabled = true;
    try {
      await setParent(button.dataset.groupDetach, '');
      refresh();
    } catch (error) {
      button.disabled = false;
      say(error.message);
    }
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (form?.id !== 'project-group-form') return;
    event.preventDefault();
    const id = decodeURIComponent(location.hash.split('/')[2] || '');
    try {
      await setParent(id, new FormData(form).get('parent') || '');
      document.activeElement?.blur?.();
      say('Saved');
      refresh();
    } catch (error) {
      say(error.message);
    }
  });
}
