// Hash routes: #/ (dashboard, the home screen), #/projects, #/projects/new
// (create wizard, lib/project-wizard.js), #/projects/<id>, #/projects/<id>/files
// (file browser, lib/project-files.js).
// Views are elements with data-view="<name>"; only the current one is shown.
// Feature modules react to navigation with onRoute(fn) from ./registry.js.
import { routeHandlers } from './registry.js';

const PROJECT_ID = /^[a-z0-9][a-z0-9-]{0,39}$/;
// A project page's tabs: #/projects/<id>[/files|/history|/settings].
export const TABS = ['overview', 'files', 'history', 'settings'];

export function parseRoute(hash) {
  const parts = String(hash || '')
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map(decodeURIComponent);
  if (parts[0] === 'projects') {
    if (parts[1] === 'new') return { view: 'projects', projectId: null, create: true, files: false, tab: 'overview' };
    const projectId = parts[1] && PROJECT_ID.test(parts[1]) ? parts[1] : null;
    const tab = projectId && TABS.includes(parts[2]) ? parts[2] : 'overview';
    return { view: 'projects', projectId, create: false, files: tab === 'files', tab };
  }
  return { view: 'dashboard', projectId: null, create: false, files: false, tab: 'overview' };
}

export function applyRoute(route, doc = globalThis.document) {
  if (!doc) return route;
  doc.querySelectorAll('[data-view]').forEach(el => {
    el.hidden = el.dataset.view !== route.view;
  });
  doc.querySelectorAll('[data-nav]').forEach(el => {
    el.classList.toggle('active', el.dataset.nav === route.view);
  });
  for (const handler of routeHandlers) {
    try {
      handler(route);
    } catch (error) {
      console.error('route handler failed', error);
    }
  }
  return route;
}

export const currentRoute = () => parseRoute(globalThis.location ? globalThis.location.hash : '');
