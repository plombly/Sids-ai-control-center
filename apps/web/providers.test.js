import test from 'node:test';
import assert from 'node:assert/strict';
import { providersMarkup } from './lib/providers.js';

test('renders routing entries', () => {
  const markup = providersMarkup({
    routing: 'builder codex/gpt-5.6-luna · reviewer claude/sonnet · repair claude/sonnet'
  });
  assert.equal((markup.match(/<tr><td>/g) || []).length, 3);
  for (const value of ['builder', 'codex/gpt-5.6-luna', 'reviewer', 'claude/sonnet', 'repair']) {
    assert.match(markup, new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  assert.match(markup, /<th>Role<\/th><th>Provider\/model<\/th>/);
});

test('renders Claude capacity', () => {
  assert.match(providersMarkup({ claude: { limit: 2, in_use: [{ holder: 'laika-worker-01', lease_expires: 123 }] } }), /Claude slots: 1\/2/);
  assert.match(providersMarkup({ claude: { limit: null, in_use: [] } }), /Claude slots: 0\/—/);
});

test('renders cooldown details', () => {
  const markup = providersMarkup({
    claude: { cooling_down: true, cooldown_reason: 'usage limit', cooldown_seconds_left: 1200 }
  });
  assert.match(markup, /cooling down/);
  assert.match(markup, /usage limit/);
  assert.match(markup, /about 20 min left/);
  assert.match(providersMarkup({ claude: { cooling_down: true, cooldown_seconds_left: null } }), /cooling down/);
  assert.doesNotMatch(providersMarkup({ claude: { cooling_down: true, cooldown_seconds_left: null } }), /min left/);
  assert.doesNotMatch(providersMarkup({ claude: { cooling_down: false } }), /cooling down/);
});

test('escapes dynamic values and handles unknown routing', () => {
  const markup = providersMarkup({
    claude: { in_use: [{ holder: '<b>x</b>' }], cooling_down: true, cooldown_reason: '<b>x</b>' }
  });
  assert.equal((markup.match(/&lt;b&gt;x&lt;\/b&gt;/g) || []).length, 2);
  assert.doesNotMatch(markup, /<b>x<\/b>/);
  assert.match(providersMarkup({ routing: null }), /<div class="empty">Routing unknown<\/div>/);
});
