import test from 'node:test';
import assert from 'node:assert/strict';
import { buildCreateBody, resultMarkup, slugify, stepMarkup, validateStep } from './lib/project-wizard.js';

const valid = {
  name: 'Web Shop',
  id: 'web-shop',
  existingIds: [],
  source: 'empty',
  importance: 'medium',
  push_remote: '',
  gate: '',
  requestId: 'request-1'
};

test('slugifies names and avoids collisions', () => {
  assert.equal(slugify('Web Shop'), 'web-shop');
  assert.equal(slugify('  My   App!!  '), 'my-app');
  assert.equal(slugify(''), 'project');
  assert.equal(slugify('New'), 'new-2');
  assert.equal(slugify('app', ['app']), 'app-2');
  assert.equal(slugify('app', ['app', 'app-2']), 'app-3');
  assert.equal(slugify('a'.repeat(60)), 'a'.repeat(40));
  assert.doesNotMatch(slugify('a'.repeat(60)), /-$/);
});

test('validates every wizard step', () => {
  assert.equal(validateStep(1, {}), 'Enter a name');
  assert.equal(validateStep(1, { ...valid, id: 'Bad ID' }), 'Choose a valid, available project ID');
  assert.equal(validateStep(1, { ...valid, id: 'web-shop', existingIds: ['web-shop'] }), 'Choose a valid, available project ID');
  assert.equal(validateStep(2, { ...valid, source: '' }), 'Choose a starting point');
  assert.equal(validateStep(2, { ...valid, source: 'clone', url: 'github.com/x/y' }), 'Enter the repository address');
  assert.equal(validateStep(3, { ...valid, importance: '' }), 'Choose an importance');
  assert.equal(validateStep(4, { ...valid, push_remote: 'not a remote' }), 'Enter a valid push remote');
  assert.equal(validateStep(4, { ...valid, gate: 'a\nb' }), 'Enter a valid test command');
  assert.equal(validateStep(4, { ...valid, gate: 'a'.repeat(201) }), 'Enter a valid test command');
  assert.equal(validateStep(1, valid), '');
  assert.equal(validateStep(2, { ...valid, source: 'clone', url: 'git@github.com:you/repo.git' }), '');
  assert.equal(validateStep(3, valid), '');
  assert.equal(validateStep(4, valid), '');
  assert.equal(validateStep(5, valid), '');
});

test('builds empty and clone request bodies', () => {
  assert.deepEqual(buildCreateBody(valid), {
    id: 'web-shop',
    name: 'Web Shop',
    importance: 'medium',
    source: 'empty',
    request_id: 'request-1'
  });
  assert.deepEqual(buildCreateBody({ ...valid, source: 'clone', url: 'git@github.com:you/repo.git', push_remote: 'https://github.com/you/repo.git', gate: 'npm test' }), {
    id: 'web-shop',
    name: 'Web Shop',
    importance: 'medium',
    source: 'clone',
    url: 'git@github.com:you/repo.git',
    push_remote: 'https://github.com/you/repo.git',
    gate: 'npm test',
    request_id: 'request-1'
  });
});

test('renders choices, clone fields, and custom ID only when revealed', () => {
  assert.match(stepMarkup(2, { source: 'empty' }), /class="choice selected"/);
  assert.doesNotMatch(stepMarkup(2, { source: 'empty' }), /name="url"/);
  assert.match(stepMarkup(2, { source: 'clone', url: 'git@github.com:you/repo.git' }), /name="url"/);
  assert.doesNotMatch(stepMarkup(1, { name: 'Web Shop', id: 'web-shop' }), /name="id"/);
  assert.match(stepMarkup(1, { name: 'Web Shop', id: 'web-shop', idRevealed: true }), /name="id"/);
});

test('renders result variants and escapes values', () => {
  assert.match(resultMarkup({ status: 'succeeded', output: JSON.stringify({ public_key: 'ssh-ed25519 AAAA' }) }, 'web-shop'), /Copy key/);
  assert.match(resultMarkup({ status: 'succeeded', output: JSON.stringify({ status: 'pending_key' }) }, 'web-shop'), /Retry import/);
  assert.doesNotMatch(resultMarkup({ status: 'succeeded' }, 'web-shop'), /Retry import|Copy key/);
  assert.match(resultMarkup({ status: 'refused', message: '<b>x</b>' }, '<b>x</b>'), /&lt;b&gt;x&lt;\/b&gt;/);
  assert.doesNotMatch(resultMarkup({ status: 'refused', message: '<b>x</b>' }, '<b>x</b>'), /<b>x<\/b>/);
  assert.match(stepMarkup(5, { ...valid, name: '<b>x</b>' }), /&lt;b&gt;x&lt;\/b&gt;/);
});
