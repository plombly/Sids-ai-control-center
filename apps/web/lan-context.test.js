import test from 'node:test';
import assert from 'node:assert/strict';
import { newRequestId, errorMessage } from './lib/api.js';
import { escValue } from './lib/format.js';
import { stepMarkup } from './lib/project-wizard.js';

const API_REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/;

test('request ids work without crypto.randomUUID (plain-http LAN pages)', () => {
  const original = globalThis.crypto.randomUUID;
  Object.defineProperty(globalThis.crypto, 'randomUUID', { value: undefined, configurable: true });
  try {
    const first = newRequestId();
    assert.match(first, API_REQUEST_ID);
    assert.notEqual(first, newRequestId());
  } finally {
    Object.defineProperty(globalThis.crypto, 'randomUUID', { value: original, configurable: true });
  }
  assert.match(newRequestId(), API_REQUEST_ID);
});

test('validation errors read as text, never [object Object]', () => {
  const body = { detail: [{ type: 'missing', loc: ['body', 'request_id'], msg: 'Field required' }] };
  assert.equal(errorMessage(body, 422), 'request_id: Field required');
  assert.equal(errorMessage({ detail: 'Project exists' }, 409), 'Project exists');
  assert.equal(errorMessage({}, 500), 'HTTP 500');
  assert.doesNotMatch(errorMessage({ detail: { a: 1 } }, 400), /object Object/);
});

test('empty inputs start empty, not with a dash', () => {
  assert.equal(escValue(''), '');
  assert.equal(escValue(undefined), '');
  assert.equal(escValue('<a "b">'), '&lt;a &quot;b&quot;&gt;');
  assert.match(stepMarkup(1, { name: '', existingIds: [] }), /name="name" value=""/);
  assert.match(stepMarkup(4, {}), /name="gate" value=""/);
  assert.doesNotMatch(stepMarkup(4, {}), /value="—"/);
});
