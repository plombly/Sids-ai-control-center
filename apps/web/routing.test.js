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

test('runs per account when there are several', () => {
  const html = routingMarkup({ roles: [], accounts: [{ provider: 'claude', account: 'work', count: 3 }, { provider: 'claude', account: 'default', count: 2 }], waits: { account_switches: 2 } });
  assert.match(html, /By account: claude work × 3 · claude default × 2/);
  assert.match(html, /2 move\(s\) to another account/);
});
