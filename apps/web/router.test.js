import test from 'node:test';
import assert from 'node:assert/strict';
import { parseRoute, applyRoute } from './lib/router.js';
import { onRoute } from './lib/registry.js';

test('routes', () => {
  assert.deepEqual(parseRoute(''), { view: 'dashboard', projectId: null, create: false });
  assert.deepEqual(parseRoute('#/'), { view: 'dashboard', projectId: null, create: false });
  assert.deepEqual(parseRoute('#/projects'), { view: 'projects', projectId: null, create: false });
  assert.deepEqual(parseRoute('#/projects/web-shop'), { view: 'projects', projectId: 'web-shop', create: false });
  assert.deepEqual(parseRoute('#/projects/Bad_Id'), { view: 'projects', projectId: null, create: false });
  assert.deepEqual(parseRoute('#/projects/new'), { view: 'projects', projectId: null, create: true });
  assert.deepEqual(parseRoute('#/nowhere'), { view: 'dashboard', projectId: null, create: false });
});

test('applyRoute shows one view, marks the nav and notifies handlers', () => {
  const views = [{ dataset: { view: 'dashboard' }, hidden: false }, { dataset: { view: 'projects' }, hidden: true }];
  const navs = ['dashboard', 'projects'].map(nav => ({ dataset: { nav }, on: null, classList: { toggle(c, on) { this.on = on; } } }));
  navs.forEach(n => (n.classList.toggle = n.classList.toggle.bind(n)));
  const doc = { querySelectorAll: sel => (sel === '[data-view]' ? views : navs) };
  const seen = [];
  onRoute(route => seen.push(route));
  applyRoute({ view: 'projects', projectId: 'web-shop' }, doc);
  assert.deepEqual(views.map(v => v.hidden), [true, false]);
  assert.deepEqual(navs.map(n => n.on), [false, true]);
  assert.deepEqual(seen.at(-1), { view: 'projects', projectId: 'web-shop' });
});
