import test from 'node:test';
import assert from 'node:assert/strict';
import { accessMarkup, loginMarkup, setupMarkup, userMenuMarkup } from './lib/auth.js';

test('first-run, sign-in and user menu screens', () => {
  const setup = setupMarkup('<oops>');
  assert.match(setup, /id="auth-setup-form"/);
  assert.match(setup, /name="code"[\s\S]*name="username"[\s\S]*name="password"[\s\S]*name="repeat"/);
  assert.match(setup, /sudo laika setup-code/);
  assert.match(setup, /Never put it on the public internet/);
  assert.match(setup, /&lt;oops&gt;/);
  assert.match(loginMarkup(), /autocomplete="current-password"[\s\S]*sudo laika reset-password/);
  assert.match(userMenuMarkup('dylan'), /dylan[\s\S]*data-sign-out/);
  assert.equal(userMenuMarkup(null), '');
});

test('access page: password form, sessions and audit log', () => {
  const html = accessMarkup(
    [{ id: 'a'.repeat(16), user: 'dylan', ip: '10.0.0.2', agent: '<Firefox>', last_seen: 1, current: true },
     { id: 'b'.repeat(16), user: 'dylan', ip: '10.8.0.6', agent: 'Safari', last_seen: 2 }],
    [{ at: 3, actor: 'user dylan', method: 'PATCH', path: '/api/projects/<x>', status: 200, ip: '10.0.0.2' }]
  );
  assert.match(html, /id="auth-password-form"/);
  assert.match(html, /this browser/);
  assert.match(html, /&lt;Firefox&gt;/);
  assert.match(html, /data-end-session="bbbbbbbbbbbbbbbb"/);
  assert.doesNotMatch(html, /data-end-session="aaaa/);
  assert.match(html, /PATCH \/api\/projects\/&lt;x&gt;/);
});
