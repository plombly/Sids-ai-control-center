import test from 'node:test';
import assert from 'node:assert/strict';
import { agoText, applyTemplate, buildsMarkup, kindCardMarkup, kindRequest, kindSettingsMarkup, sizeText } from './lib/project-kinds.js';
import { projectDetailMarkup, tabsMarkup } from './lib/projects.js';

const catalog = {
  types: {
    game: { label: 'Game', icon: '🎮', runs: false, templates: [{ title: 'Add a level', text: 'Add a level with ___.' }] },
    website: { label: 'Website', icon: '🌐', runs: true, templates: [{ title: 'New page', text: 'Add a <page> ___' }] },
    other: { label: 'Something else', icon: '🧩', templates: [{ title: 'Add a feature', text: 'Add ___.' }] }
  },
  recipes: { love2d: { label: 'LÖVE (.love file)' }, static: { label: 'Static site' }, unity: { label: 'Unity', unsupported: 'Needs Unity' } }
};

test('a detected game gets its templates, the evidence and a Build button', () => {
  const html = kindCardMarkup({ id: 'g', project_type: { type: 'game', stack: 'love2d', source: 'detected', evidence: ['main.lua <x>'] } }, catalog);
  assert.match(html, /🎮/);
  assert.match(html, /LÖVE/);
  assert.match(html, /main\.lua &lt;x&gt;/);
  assert.match(html, /data-build-start="g"/);
  assert.match(html, /data-goal-template="Add a level with ___\."/);
});

test('a website gets its own ideas and no Build button; unsupported stacks cannot build', () => {
  const site = kindCardMarkup({ id: 's', project_type: { type: 'website', stack: 'static', source: 'chosen' } }, catalog);
  assert.doesNotMatch(site, /data-build-start/);
  assert.match(site, /data-goal-template="Add a &lt;page&gt; ___"/);
  assert.match(site, /set by you/);
  const unity = kindCardMarkup({ id: 'u', project_type: { type: 'game', stack: 'unity' } }, catalog);
  assert.doesNotMatch(unity, /data-build-start/);
});

test('unknown projects say so, link to Settings and still offer general ideas', () => {
  const html = kindCardMarkup({ id: 'x', project_type: { type: 'unknown' } }, catalog);
  assert.match(html, /Not recognised yet/);
  assert.match(html, /#\/projects\/x\/settings/);
  assert.match(html, /Add a feature/);
  assert.match(kindCardMarkup({ id: 'x', project_type: { type: 'checking' } }, catalog), /Looking at the project/);
  assert.doesNotMatch(kindCardMarkup({ id: 'sid', project_type: { type: 'unknown' } }, catalog), /data-kind-recheck/);
});

test('type settings: automatic option names the detected type; SID has no build fields', () => {
  const html = kindSettingsMarkup({ id: 'g', build_image: 'node:22', project_type: { detected: 'game', chosen: 'website', description: 'a <b> site' } }, catalog);
  assert.match(html, /Automatic \(Game\)/);
  assert.match(html, /<option value="website" selected>/);
  assert.match(html, /a &lt;b&gt; site/);
  assert.match(html, /value="node:22"/);
  assert.doesNotMatch(kindSettingsMarkup({ id: 'g', project_type: {} }, catalog), /—/);
  assert.doesNotMatch(kindSettingsMarkup({ id: 'sid', project_type: {} }, catalog), /build_command/);
  assert.deepEqual(kindRequest({ type: '', type_description: ' a  bot\n', build_image: ' x ' }), { type: '', type_description: 'a bot', build_image: 'x' });
});

test('builds list: download, log, errors and the running state', () => {
  const now = 10_000;
  const html = buildsMarkup('g', {
    recipe: { label: 'LÖVE', command: 'zip <x>', image: 'alpine:3', note: 'first build is slow' },
    builds: [
      { id: 'b2', status: 'running', requested_at: now - 30 },
      { id: 'b1', status: 'succeeded', commit: 'abcdef123456', size: 2_500_000, started_at: now - 100, finished_at: now - 80, requested_at: now - 120 },
      { id: 'b0', status: 'failed', error: 'exit <2>' }
    ]
  }, now);
  assert.match(html, /zip &lt;x&gt;/);
  assert.match(html, /disabled>Building…/);
  assert.match(html, /href="\/api\/projects\/g\/builds\/b1\/download" download>Download \(2\.5 MB\)/);
  assert.match(html, /main abcdef12 · 20 s/);
  assert.match(html, /exit &lt;2&gt;/);
  assert.doesNotMatch(html, /—/); // no placeholder dashes for empty values
  assert.doesNotMatch(html, /builds\/b2\/download/);
  const refused = buildsMarkup('u', { recipe: { unsupported: 'Needs a Mac' }, builds: [] });
  assert.match(refused, /Needs a Mac/);
  assert.doesNotMatch(refused, /data-build-start/);
  assert.match(refused, /No builds yet/);
});

test('templates select their first blank; helpers format sizes and ages', () => {
  const box = { value: '', focus() {}, setSelectionRange(a, b) { this.range = [a, b]; } };
  applyTemplate(box, 'Add ___ to ___');
  assert.deepEqual(box.range, [4, 7]);
  assert.equal(sizeText(1500), '2 KB');
  assert.equal(agoText(100, 160), 'just now');
  assert.equal(agoText(0, 160), '');
});

test('projects get a Builds tab (not SID) and the overview shows the type card', () => {
  assert.match(tabsMarkup('g', 'builds'), /href="#\/projects\/g\/builds" class="active"/);
  assert.doesNotMatch(tabsMarkup('sid', 'overview'), /builds/);
  const html = projectDetailMarkup({ id: 'g', name: 'G', catalog, project_type: { type: 'game', stack: 'love2d' } }, 'overview');
  assert.match(html, /kind-card[\s\S]*project-goal-form/);
});
