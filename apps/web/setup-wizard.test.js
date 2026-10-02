import test from 'node:test';
import assert from 'node:assert/strict';
import { STEPS, providerCard, stepChanges, stepMarkup, wizardMarkup } from './lib/setup-wizard.js';
import { parseRoute } from './lib/router.js';

test('setup route and every step renders', () => {
  assert.equal(parseRoute('#/setup').view, 'setup');
  for (const [id] of STEPS) {
    const html = wizardMarkup(id, { values: {}, providers: {}, info: { cpus: 8, memory_gb: 16, suggested_workers: 8, public_addresses: [] } });
    assert.match(html, /id="setup-form"/, id);
    assert.equal(html.includes('data-setup-skip'), id !== 'done', id);
  }
  const capacity = stepMarkup('capacity', { values: {}, info: { cpus: 8, memory_gb: 16, suggested_workers: 6 } });
  assert.match(capacity, /name="MAX_WORKERS" value="6"/);
  assert.match(capacity, /<option value="true" selected>Automatic/);
});

test('the safety step warns about public addresses', () => {
  assert.match(stepMarkup('safety', { info: { public_addresses: ['93.184.216.34'] } }), /public address \(93\.184\.216\.34\)/);
  assert.match(stepMarkup('safety', { info: { public_addresses: [] } }), /Looks private/);
  assert.match(stepMarkup('safety', {}), /never the public internet/);
});

test('provider cards: status, device code, Claude code box, saved key', () => {
  assert.match(providerCard('claude', 'Claude', { signed_in: true, plan: 'pro' }), /Signed in · pro/);
  assert.match(providerCard('codex', 'Codex', { installed: false }), /Not installed/);
  const codex = providerCard('codex', 'Codex', {}, { state: 'waiting', url: 'https://auth.openai.com/codex/device', code: 'ABCD-12345' });
  assert.match(codex, /href="https:\/\/auth\.openai\.com\/codex\/device"[\s\S]*ABCD-12345/);
  assert.doesNotMatch(codex, /claude_code/);
  const claude = providerCard('claude', 'Claude', {}, { state: 'waiting', url: 'https://claude.ai/oauth?a=1&b="x"' }, true);
  assert.match(claude, /name="claude_code"/);
  assert.match(claude, /&quot;x&quot;/);
  assert.match(claude, /API key instead \(saved\)/);
  assert.match(providerCard('claude', 'Claude', {}, { state: 'failed', message: '<bad>' }), /&lt;bad&gt;/);
});

test('steps only send their own settings', () => {
  assert.deepEqual(stepChanges('welcome', { SERVER_NAME: 'Home', THEME: 'dark', ACCENT: '#3fcf8e', other: 'x' }), { SERVER_NAME: 'Home', THEME: 'dark', ACCENT: '#3fcf8e' });
  assert.deepEqual(stepChanges('capacity', { AUTOSCALE: 'true', MAX_WORKERS: '7', WORKER_COUNT: '4', CLAUDE_MAX_CONCURRENT: '' }), { AUTOSCALE: 'true', MAX_WORKERS: '7', WORKER_COUNT: '4' });
  assert.deepEqual(stepChanges('backups', { BACKUP_TIME: '02:00', BACKUP_KEEP: '7' }), { BACKUP_TIME: '02:00', BACKUP_KEEP: '7', BACKUP_REMOTE: '' });
  assert.deepEqual(stepChanges('notify', { discord_webhook: 'x' }), {});
});
