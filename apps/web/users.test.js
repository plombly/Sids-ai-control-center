import test from 'node:test';
import assert from 'node:assert/strict';
import { accessFields, accessSummary, editMarkup, grantable, usersMarkup } from './lib/users.js';
import { inviteMarkup, inviteToken } from './lib/auth.js';
import { can } from './lib/access.js';
import { tabsMarkup } from './lib/projects.js';
import { needsYou } from './lib/home.js';
import { navMarkup } from './lib/system-settings.js';

const projects = [{ id: 'laika', name: 'LAIka', view_only: true }, { id: 'shop', name: 'Shop' }, { id: 'shop-api', name: 'Shop API', parent: 'shop' }, { id: 'app', name: 'App', view_only: true }];

test('users page: people, access, invite link, never the builder-managed projects', () => {
  const users = { users: [
    { name: 'alex', username: 'Alex', role: 'admin', access: {}, last_seen: 1 },
    { name: 'sam', username: 'Sam', role: 'member', access: { shop: 'build' }, invited: true },
    { name: 'kim', username: '<Kim>', role: 'member', access: {}, disabled: true }
  ] };
  const html = usersMarkup(users, projects, 'alex', { invite: { path: '#/invite/abcdefghijkl' }, username: 'Sam' });
  assert.match(html, /<b>Alex<\/b> <span class="subtle">\(you\)<\/span>/);
  assert.doesNotMatch(html, /data-user-remove="alex"/);          // never yourself
  assert.match(html, /Shop: Build/);
  assert.match(html, /&lt;Kim&gt;/);
  assert.match(html, /<span class="pill warn">invited<\/span>/);
  assert.match(html, /\/#\/invite\/abcdefghijkl/);
  assert.deepEqual(grantable(projects).map(p => p.id), ['shop', 'shop-api']);
  assert.match(accessFields(projects, { shop: 'approve' }), /name="access:shop"[\s\S]*?<option value="approve" selected>Approve/);
  assert.match(accessFields(projects), / ↳ Shop API/);
  assert.equal(accessSummary({ role: 'admin' }), 'Everything (administrator)');
  assert.match(editMarkup({ name: 'alex', username: 'Alex', role: 'admin', access: {} }, projects), /data-member-only hidden/);
});

test('joining with an invite link', () => {
  assert.equal(inviteToken('#/invite/AbC_123-xyz0'), 'AbC_123-xyz0');
  assert.equal(inviteToken('#/projects'), '');
  assert.match(inviteMarkup('Sam'), /invited as <b>Sam<\/b>[\s\S]*name="repeat"/);
  assert.match(inviteMarkup(''), /no longer works/);
});

test('screens follow what the person may do', () => {
  assert.equal(can({ my_access: 'build' }, 'approve'), false);
  assert.equal(can({ my_access: 'approve' }, 'build'), true);
  assert.equal(can({}, 'approve'), true);                      // older API: everyone is the administrator
  assert.doesNotMatch(tabsMarkup('shop', 'overview', false, { my_access: 'build' }), /\/settings"/);
  assert.match(tabsMarkup('shop', 'overview', false, { my_access: 'approve' }), /\/settings"/);
  const ready = { id: 'j1', project_id: 'shop', status: 'awaiting_review', review_verdict: 'pass' };
  const stuck = { id: 'j2', project_id: 'shop', status: 'needs_human' };
  const asBuilder = needsYou({ approvals: [ready], jobs: [stuck], projects: [{ id: 'shop', my_access: 'build' }] });
  assert.deepEqual([asBuilder.ready.length, asBuilder.stuck.length], [0, 1]);
  const asViewer = needsYou({ approvals: [ready], jobs: [stuck], projects: [{ id: 'shop', my_access: 'view' }] });
  assert.deepEqual([asViewer.ready.length, asViewer.stuck.length], [0, 0]);
  const memberNav = navMarkup({ sections: [{ id: 'general', label: 'General' }] }, 'access', false);
  assert.match(memberNav, /settings\/access/);
  assert.doesNotMatch(memberNav, /settings\/(general|users|system|notifications)/);
  assert.match(navMarkup({ sections: [] }, 'users', true), /settings\/users/);
});

test('top-level projects have no prefix in the access list', () => {
  const html = accessFields([{ id: 'shop', name: 'Shop' }, { id: 'api', name: 'API', parent: 'shop' }]);
  assert.match(html, /<span>Shop<\/span>/);
  assert.match(html, /<span> ↳ API<\/span>/);
});
