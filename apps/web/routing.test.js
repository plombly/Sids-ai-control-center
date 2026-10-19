import { test } from 'node:test';
import assert from 'node:assert/strict';
import { routingMarkup } from './lib/routing.js';

test('routing shows runs per role, switches with reasons, waits and scaling', () => {
  const html = routingMarkup({ days: 7, roles: [{ role: 'reviewer', runs: [{ provider: 'claude', model: 'sonnet', count: 4 }, { provider: 'codex', model: '', count: 1 }] }],
    fallbacks: [{ job: 'b1', role: 'reviewer', at: 1790000000, reason: 'claude unavailable: quota' }], waits: { provider_waits: 2 },
    scaler: [{ at: 1790000000, reason: 'jobs waiting', target: 3 }] });
  assert.match(html, /claude <span class="subtle">sonnet<\/span> × 4<br>codex × 1/);
  assert.match(html, /reviewer <code>b1<\/code>: claude unavailable: quota/);
  assert.match(html, /2 wait\(s\) for a paused provider/);
  assert.match(html, /jobs waiting → 3 worker\(s\)/);
  assert.match(routingMarkup({}), /Never: every run used its usual provider/);
});
