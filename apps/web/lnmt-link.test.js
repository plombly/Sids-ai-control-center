import { test } from 'node:test';
import assert from 'node:assert/strict';
import { connectDialogMarkup, connectState, deployCardMarkup, lnmtCardMarkup } from './lib/lnmt-link.js';

test('Settings card: connect with a pairing code, or show the connection without secrets', () => {
  assert.match(lnmtCardMarkup({}), /data-lnmt-connect.*name="code"/s);
  const on = lnmtCardMarkup({ connected: true, name: 'LNMT', url: 'https://10.0.0.232:8443' });
  assert.match(on, /Connected to <b>LNMT<\/b> at <code>https:\/\/10\.0\.0\.232:8443/);
  assert.match(on, /data-lnmt-disconnect/);
});

test('Deploy to LNMT: nothing when not connected; the current target; only git-built apps to pick', () => {
  assert.equal(deployCardMarkup('shop', { connected: false }), '');
  assert.match(deployCardMarkup('shop', { connected: true, target: null }), /data-lnmt-apps/);
  assert.match(deployCardMarkup('shop', { connected: true, target: { app: 'site' } }), /Deploys to <b>site<\/b>/);
  const picked = deployCardMarkup('shop', { connected: true }, [
    { customer: 'sam', name: 'site', git_url: 'ssh://c-sam@n/site.git', sites: ['devyn.example.com'] },
    { customer: 'sam', name: 'db', git_url: '' }]);
  assert.match(picked, /<option value="sam\/site">site \(devyn\.example\.com\)<\/option>/);
  assert.doesNotMatch(picked, /value="sam\/db"/);
  assert.match(deployCardMarkup('shop', { connected: true }, []), /no LNMT apps built from git/);
});

test('#/connect: the state is read strictly; the dialog names the person', () => {
  assert.equal(connectState('#/connect?state=abcdefghijklmnop1234'), 'abcdefghijklmnop1234');
  assert.equal(connectState('#/connect?state=short'), null);
  assert.equal(connectState('#/projects'), null);
  assert.match(connectDialogMarkup('Devyn', 'LNMT'), /sign you in as <b>Devyn<\/b>/);
  assert.match(connectDialogMarkup('<b>', 'LNMT'), /&lt;b&gt;/);
});
