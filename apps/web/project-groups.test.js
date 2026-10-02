import test from 'node:test';
import assert from 'node:assert/strict';
import { groupOverviewMarkup, groupSettingsMarkup, nestProjects, partOfMarkup } from './lib/project-groups.js';
import { projectsMarkup } from './lib/home.js';
import { projectDetailMarkup } from './lib/projects.js';
import { kindSettingsMarkup } from './lib/project-kinds.js';
import { stepMarkup } from './lib/project-wizard.js';

const projects = [
  { id: 'shop', name: 'Shop', status: 'active', counts: {}, children: [{ id: 'shop-app', name: '<App>' }] },
  { id: 'shop-app', name: '<App>', status: 'active', parent: 'shop', parent_name: 'Shop', counts: {} },
  { id: 'blog', name: 'Blog', status: 'active', counts: {} },
  { id: 'laika', name: 'LAIka', status: 'active', counts: {} }
];

test('children are nested under their parent everywhere projects are listed', () => {
  const nested = nestProjects(projects);
  assert.deepEqual(nested.map(p => p.id), ['shop', 'blog', 'laika']);
  assert.deepEqual(nested[0].members.map(p => p.id), ['shop-app']);
  const html = projectsMarkup(projects);
  assert.match(html, /card-children[\s\S]*href="#\/projects\/shop-app">↳ &lt;App&gt;/);
  assert.equal((html.match(/class="home-project"/g) || []).length, 3);
  // A child whose parent is not in the list still shows on its own.
  assert.deepEqual(nestProjects([projects[1]]).map(p => p.id), ['shop-app']);
});

test('a child page says where it belongs and its inherited settings are locked', () => {
  assert.match(partOfMarkup(projects[1]), /Part of <a href="#\/projects\/shop">Shop<\/a>/);
  assert.equal(partOfMarkup(projects[2]), '');
  const page = projectDetailMarkup({ ...projects[1], goals: [], jobs: [] }, 'overview');
  assert.match(page, /id="project-importance"[^>]*disabled/);
  assert.doesNotMatch(page, /Child projects|New child project/);
  const settings = kindSettingsMarkup({ ...projects[1], project_type: {} }, { types: {}, recipes: {} });
  assert.match(settings, /name="gate_network" disabled/);
  assert.match(settings, /Tests follow the parent project, Shop/);
});

test('a parent lists its children and can add more; LAIka can be a parent', () => {
  const html = groupOverviewMarkup(projects[0]);
  assert.match(html, /Child projects/);
  assert.match(html, /href="#\/projects\/shop-app">&lt;App&gt;/);
  assert.match(html, /data-group-new-child="shop"/);
  assert.match(groupOverviewMarkup(projects[2]), /Project group[\s\S]*New child project/);
  assert.match(groupOverviewMarkup(projects[3]), /data-group-new-child="laika"/);
  assert.equal(groupSettingsMarkup(projects[3], projects), ''); // LAIka is never a child
});

test('group settings: pick a parent, or detach children', () => {
  const blog = groupSettingsMarkup(projects[2], projects);
  assert.match(blog, /<option value="shop">Shop<\/option>/);
  assert.match(blog, /<option value="laika">LAIka<\/option>/);
  assert.doesNotMatch(blog, /value="shop-app"|value="blog"/); // no children or itself
  const app = groupSettingsMarkup(projects[1], projects);
  assert.match(app, /<option value="shop" selected>/);
  const parent = groupSettingsMarkup(projects[0], projects);
  assert.match(parent, /data-group-detach="shop-app"/);
  assert.doesNotMatch(parent, /<select/);
});

test('the wizard shows the group a new child project joins', () => {
  assert.match(stepMarkup(3, { parent: 'shop', parentName: 'Shop', importance: 'medium' }), /child of Shop/);
  assert.match(stepMarkup(3, { importance: 'medium' }), /choice-grid/);
  assert.match(stepMarkup(5, { name: 'App', id: 'app', parent: 'shop', parentName: 'Shop', importance: 'medium' }), /Part of: Shop/);
});
