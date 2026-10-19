import { test } from 'node:test';
import assert from 'node:assert/strict';
import { accountsMarkup, stateMarkup, progressMarkup, slug } from './lib/provider-accounts.js';

const base = { kind: 'login', enabled: true, signed_in: true, running: 1, max_concurrent: 2, hold: { held: false } };

test('each account with its state, limit and actions; the default cannot be removed', () => {
  const html = accountsMarkup({ accounts: {
    claude: [{ ...base, id: 'default', label: 'Default' }, { ...base, id: 'work', label: 'Work plan', usage: { windows: { five_hour: { percent: 41.6 } } } }],
    codex: [{ ...base, id: 'default', label: 'Default', max_concurrent: 0 }, { ...base, id: 'second', label: 'Second', kind: 'key', signed_in: false, key_saved: false }]
  } });
  assert.match(html, /Work plan<\/b> <span class="subtle">work/);
  assert.match(html, /42% of plan used/);
  assert.match(html, /now 1 \(no limit\)/);
  assert.match(html, /data-account-remove="claude:work"/);
  assert.doesNotMatch(html, /data-account-remove="claude:default"/);
  assert.match(html, /data-account-key="codex:second">Save key/);
  assert.match(html, /No key yet/);
  assert.match(html, /data-account-add>Add account/);
  assert.match(html, /name="stop-claude:work"/);
  assert.doesNotMatch(html, /name="stop-codex:/);   // only Claude reports plan usage
});

test('states, sign-in progress for the right account, ids from names', () => {
  assert.match(stateMarkup({ ...base, hold: { held: true, until: 1792400000, reason: 'quota' } }), /Paused until/);
  assert.match(stateMarkup({ ...base, enabled: false }), /Turned off/);
  const waiting = { state: 'waiting', url: 'https://claude.ai/x', account: 'work' };
  assert.match(progressMarkup(waiting, 'claude', 'work'), /Open the sign-in page.*data-account-code/s);
  assert.equal(progressMarkup(waiting, 'claude', 'default'), '');
  assert.equal(slug('My  Second Plan!'), 'my-second-plan');
});
