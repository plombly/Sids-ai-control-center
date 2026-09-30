import test from 'node:test';
import assert from 'node:assert/strict';
import { overviewMarkup } from './lib/project-overview.js';

const project = (overrides = {}) => ({
  id: 'alpha',
  name: 'Alpha',
  importance: 'high',
  counts: {
    jobs_merged: 3,
    jobs_running: 1,
    jobs_queued: 2,
    jobs_awaiting_approval: 1,
    jobs_needs_human: 1
  },
  stats: { remaining_effort: 5 },
  ...overrides
});

test('renders project rows, links, importance, and progress', () => {
  const markup = overviewMarkup([project(), project({ id: 'two words', name: 'Beta' })]);
  assert.equal((markup.match(/<tbody>/g) || []).length, 1);
  assert.equal((markup.match(/<tbody>[\s\S]*<\/tbody>/)?.[0].match(/<tr>/g) || []).length, 2);
  assert.match(markup, /href="#\/projects\/alpha">Alpha<\/a>/);
  assert.match(markup, /href="#\/projects\/two%20words">Beta<\/a>/);
  assert.match(markup, /class="pill[^\"]*">high<\/span>/);
  assert.match(markup, /3 merged \/ 5 remaining/);
});

test('renders a dash for missing remaining effort and skips archived projects', () => {
  const markup = overviewMarkup([
    project({ status: 'archived', name: 'Old' }),
    project({ stats: null, name: 'Current' }),
    project({ id: 'missing-stats', name: 'Missing stats', stats: undefined, counts: null })
  ]);
  assert.doesNotMatch(markup, /Old/);
  assert.match(markup, /Current/);
  assert.match(markup, /3 merged \/ — remaining/);
  assert.match(markup, /Missing stats/);
});

test('shows attention pills only for positive counts', () => {
  const markup = overviewMarkup([
    project({ counts: { jobs_merged: 0, jobs_awaiting_approval: 1, jobs_needs_human: 1 }, stats: {} }),
    project({ id: 'quiet', counts: { jobs_merged: 0, jobs_awaiting_approval: 0, jobs_needs_human: 0 }, stats: {} })
  ]);
  assert.equal((markup.match(/waiting on you/g) || []).length, 1);
  assert.equal((markup.match(/needs you/g) || []).length, 1);
});

test('renders empty and all-archived project lists', () => {
  assert.equal(overviewMarkup([]), '<div class="empty">No projects</div>');
  assert.equal(overviewMarkup([project({ status: 'archived' })]), '<div class="empty">No projects</div>');
});

test('escapes names and preserves input order', () => {
  const markup = overviewMarkup([
    project({ id: 'first', name: '<b>x</b>' }),
    project({ id: 'second', name: 'Second' })
  ]);
  assert.doesNotMatch(markup, /<b>x<\/b>/);
  assert.match(markup, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.ok(markup.indexOf('first') < markup.indexOf('second'));
});
