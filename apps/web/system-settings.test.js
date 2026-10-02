import test from 'node:test';
import assert from 'node:assert/strict';
import { changedValues, fieldMarkup, navMarkup, pendingMarkup, sectionMarkup, systemMarkup } from './lib/system-settings.js';
import { applyPrefs } from './lib/appearance.js';
import { clockText } from './lib/elapsed.js';

const schema = {
  sections: [{ id: 'general', label: 'General', help: 'g' }, { id: 'appearance', label: 'Appearance', help: 'a' }, { id: 'ai', label: 'AI & pipeline', help: 'x' }],
  fields: [
    { key: 'THEME', section: 'appearance', label: 'Theme', type: 'choice', default: 'system', choices: ['system', 'dark', 'light'], apply: 'live' },
    { key: 'ACCENT', section: 'appearance', label: 'Accent', type: 'color', default: '#5b9dff', presets: ['#5b9dff', '#3fcf8e'], apply: 'live' },
    { key: 'REDUCE_MOTION', section: 'appearance', label: 'Reduce motion', type: 'bool', default: 'false', apply: 'live' },
    { key: 'HOME_SECTIONS', section: 'appearance', label: 'Sections', type: 'list', default: 'needs,recent', choices: ['needs', 'recent', 'projects'], choice_labels: { needs: 'Needs you' }, apply: 'live' },
    { key: 'MAX_REPAIR_ATTEMPTS', section: 'ai', label: 'Repairs <b>', type: 'int', default: '2', min: 0, max: 10, apply: 'restart', help: 'h' }
  ]
};

test('navigation lists schema sections and the extra pages', () => {
  const html = navMarkup(schema, 'ai');
  assert.match(html, /href="#\/settings\/notifications"/);
  assert.match(html, /href="#\/settings\/phones"/);
  assert.match(html, /href="#\/settings\/system"/);
  assert.match(html, /href="#\/settings\/ai" class="active"/);
});

test('fields render by type with current values, escaping and apply notes', () => {
  const html = sectionMarkup(schema, 'appearance', { THEME: 'light', ACCENT: '#3fcf8e', REDUCE_MOTION: 'true', HOME_SECTIONS: 'recent' });
  assert.match(html, /<option value="light" selected>/);
  assert.match(html, /type="color"[^>]*value="#3fcf8e"/);
  assert.match(html, /class="swatch chosen"[^>]*data-swatch="#3fcf8e"/);
  assert.match(html, /type="checkbox"[^>]*checked/);
  assert.match(html, /value="recent" checked/);
  assert.match(html, /Needs you/);
  const ai = fieldMarkup(schema.fields[4], '3');
  assert.match(ai, /Repairs &lt;b&gt;/);
  assert.match(ai, /type="number"[^>]*value="3"[^>]*min="0" max="10"/);
  assert.match(ai, /Applies when services restart/);
  assert.equal(sectionMarkup(schema, 'nope', {}), '');
});

test('pending settings offer Apply; system page explains branding', () => {
  assert.match(pendingMarkup(schema, ['MAX_REPAIR_ATTEMPTS']), /Repairs &lt;b&gt;[\s\S]*data-settings-apply/);
  assert.equal(pendingMarkup(schema, []), '');
  assert.match(systemMarkup({ commit: 'abc123' }), /branding\.json[\s\S]*laika branding apply/);
});

test('only changed values are sent', () => {
  const rows = [
    { dataset: { key: 'THEME' }, querySelector: sel => (sel === '[data-list]' ? null : { type: 'select-one', value: 'dark' }) },
    { dataset: { key: 'REDUCE_MOTION' }, querySelector: sel => (sel === '[data-list]' ? null : { type: 'checkbox', checked: false }) }
  ];
  const form = { querySelectorAll: () => rows };
  assert.deepEqual(changedValues(form, { THEME: 'system', REDUCE_MOTION: 'false' }), { THEME: 'dark' });
});

test('appearance and clock preferences', () => {
  const merged = applyPrefs({ THEME: 'light', CLOCK: '12h' }, null);
  assert.equal(merged.THEME, 'light');
  const at = new Date(2026, 9, 1, 15, 4).getTime() / 1000;
  assert.equal(clockText(at, at + 60), '3:04 PM');
  applyPrefs({ CLOCK: '24h', DATE_FORMAT: 'iso' }, null);
  assert.equal(clockText(at, at + 60), '15:04');
  assert.equal(clockText(at, at + 86400 * 3), '2026-10-01 15:04');
  applyPrefs({}, null);
});

test('updates card: up to date, available, running, failed, not configured', async () => {
  const { updateMarkup, systemMarkup } = await import('./lib/system-settings.js');
  assert.match(updateMarkup({ available: { newer: false, checked_at: 1 } }), /Up to date/);
  const html = updateMarkup({ available: { newer: true, latest: '1.0.1', current: '1.0.0', notes: '<fixes>' } });
  assert.match(html, /LAIka 1\.0\.1<\/b> is available \(you have 1\.0\.0\)/);
  assert.match(html, /data-system-update/);
  assert.match(html, /&lt;fixes&gt;/);
  assert.match(updateMarkup({ status: { state: 'waiting' } }), /Waiting for running jobs to finish/);
  assert.doesNotMatch(updateMarkup({ status: { state: 'waiting' }, available: { newer: true } }), /data-system-update/);
  assert.match(updateMarkup({ status: { state: 'failed', message: 'x' } }), /Last update failed: x/);
  assert.match(updateMarkup({ available: { configured: false } }), /no update address/);
  assert.match(systemMarkup({ version: '1.0.0' }, { available: { newer: false } }), /id="update-card"/);
  assert.doesNotMatch(systemMarkup({ version: '1.0.0' }), /update-card/);
});
