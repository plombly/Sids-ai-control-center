import test from 'node:test';
import assert from 'node:assert/strict';
import { healthMarkup } from './lib/system-health.js';

test('renders overall status and age', () => {
  const markup = healthMarkup({ report: { status: 'ok', age_seconds: 42, checks: [] }, backup: null });
  assert.match(markup, /class="pill[^"]*">ok<\/span>/);
  assert.match(markup, /checked 42s ago/);
});

test('marks stale reports only after 360 seconds', () => {
  const stale = healthMarkup({ report: { status: 'warn', age_seconds: 400, checks: [] }, backup: null });
  const fresh = healthMarkup({ report: { status: 'ok', age_seconds: 100, checks: [] }, backup: null });
  assert.match(stale, /Watchdog report is stale/);
  assert.doesNotMatch(fresh, /Watchdog report is stale/);
});

test('orders checks by level while preserving level order', () => {
  const markup = healthMarkup({
    report: {
      status: 'fail',
      age_seconds: 1,
      checks: [
        { name: 'warn-one', level: 'warn', detail: 'w1' },
        { name: 'ok-one', level: 'ok', detail: 'o1' },
        { name: 'fail-one', level: 'fail', detail: 'f1' },
        { name: 'warn-two', level: 'warn', detail: 'w2' },
        { name: 'fail-two', level: 'fail', detail: 'f2' }
      ]
    },
    backup: null
  });
  const positions = ['fail-one', 'fail-two', 'warn-one', 'warn-two', 'ok-one'].map(name => markup.indexOf(name));
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
});

test('formats backups and lists failed error names', () => {
  const markup = healthMarkup({
    report: null,
    backup: { at: '20260930T192958Z', ok: false, path: '/tmp/backup', errors: { alpha: 'x', beta: 'y' } }
  });
  assert.match(markup, /2026-09-30 19:29 UTC/);
  assert.match(markup, /class="pill[^"]*">failed<\/span>/);
  assert.match(markup, /alpha, beta/);
});

test('renders empty report and backup messages', () => {
  const markup = healthMarkup({ report: null, backup: null });
  assert.match(markup, /No watchdog report yet \(is sid-ai-watchdog\.timer running\?\)/);
  assert.match(markup, /No backup recorded yet/);
});

test('escapes check details', () => {
  const markup = healthMarkup({
    report: { status: 'ok', age_seconds: null, checks: [{ name: 'detail', level: 'ok', detail: '<b>x</b>' }] },
    backup: null
  });
  assert.doesNotMatch(markup, /<b>x<\/b>/);
  assert.match(markup, /&lt;b&gt;x&lt;\/b&gt;/);
});
