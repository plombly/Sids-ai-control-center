import { test } from 'node:test';
import assert from 'node:assert/strict';
import { availabilityMarkup, holdMarkup, usageMarkup } from './lib/provider-status.js';

test('a paused provider shows why and until when; a free one says it takes work', () => {
  const until = Date.now() / 1000 + 3600;
  assert.match(holdMarkup('Codex', { held: true, kind: 'quota', reason: "You've hit your usage limit", until }), /Usage limit reached.*paused until/);
  assert.match(holdMarkup('Claude', { held: false }), /Taking work/);
  assert.match(holdMarkup('Claude', { held: true, kind: 'auth', until }), /Needs signing in again/);
});

test('Claude plan usage shows each window with a warning colour from 80%', () => {
  const html = usageMarkup({ windows: { five_hour: { percent: 40, resets_at: 2000000000 }, seven_day: { percent: 85 } } });
  assert.match(html, /5-hour window: 40% used/);
  assert.match(html, /class="spend warn"><span>7-day window: 85% used/);
  assert.equal(usageMarkup(null), '');
  assert.match(availabilityMarkup({}), /Claude.*Taking work.*Codex.*Taking work/s);
});
