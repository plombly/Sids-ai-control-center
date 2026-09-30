import test from 'node:test';
import assert from 'node:assert/strict';
import { insightsMarkup } from './lib/pipeline-insights.js';

test('waiting for files includes the job, reason, and detail button', () => {
  const output = insightsMarkup([{ id: 'wait-1', blocked_reason: 'missing input' }]);
  assert.match(output, /wait-1/);
  assert.match(output, /missing input/);
  assert.match(output, /data-detail="wait-1"/);
});

test('provider waits and fallbacks include provider/model and messages', () => {
  const output = insightsMarkup([
    { id: 'wait', provider: 'codex', model: 'o4', provider_wait: 'queued' },
    { id: 'fallback', provider: 'claude', provider_fallback: 'retrying elsewhere' }
  ]);
  assert.match(output, /codex\/o4/);
  assert.match(output, /claude\/—/);
  assert.match(output, /queued/);
  assert.match(output, /retrying elsewhere/);
});

test('best-of builds render selected and side details', () => {
  const output = insightsMarkup([
    {
      id: 'best',
      best_of: {
        chosen: 'alt',
        primary: { provider: 'codex', tests: false, lines: 10 },
        alt: { provider: 'claude', model: 'sonnet', tests: true, lines: 7, cost_usd: 0.2 }
      }
    },
    { id: 'skipped', best_of: { skipped: 'no failure to retry' } }
  ]);
  for (const value of ['alt', 'codex', 'claude', 'tests: yes', 'tests: no', '10', '7', 'no failure to retry']) {
    assert.match(output, new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
});

test('specialist reviews render verdict pills', () => {
  const output = insightsMarkup([{ id: 'review', review_aspects: { spec: 'pass', safety: 'changes_required' } }]);
  assert.match(output, /<span class="pill[^>]*">spec: pass<\/span>/);
  assert.match(output, /<span class="pill[^>]*">safety: changes_required<\/span>/);
});

test('plain jobs do not appear in insight sections', () => {
  const output = insightsMarkup([{ id: 'plainjob', status: 'running' }]);
  assert.doesNotMatch(output, /plainjob/);
  assert.equal(insightsMarkup([{ id: 'plainjob' }]).match(/None/g).length, 4);
});

test('empty input renders all titles and empty sections', () => {
  const output = insightsMarkup([]);
  for (const title of ['Waiting for files', 'Provider waits and fallbacks', 'Best-of builds', 'Specialist reviews']) {
    assert.match(output, new RegExp(title));
  }
  assert.equal((output.match(/<div class="empty">None<\/div>/g) || []).length, 4);
});

test('rendered values are escaped', () => {
  const output = insightsMarkup([
    { id: 'blocked', blocked_reason: '<b>x</b>' },
    { id: 'fallback', provider_fallback: '<b>x</b>' }
  ]);
  assert.equal((output.match(/&lt;b&gt;x&lt;\/b&gt;/g) || []).length, 2);
  assert.doesNotMatch(output, /<b>x<\/b>/);
});

test('null and invalid inputs render four empty sections', () => {
  for (const value of [null, undefined, [null, 5]]) {
    const output = insightsMarkup(value);
    assert.equal((output.match(/<div class="empty">None<\/div>/g) || []).length, 4);
  }
});
