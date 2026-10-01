import test from 'node:test';
import assert from 'node:assert/strict';
import { logLineMarkup } from './lib/job-log.js';
import { jobDetailMarkup } from './lib/markup.js';

test('log lines: kinds, exit codes, escaping', () => {
  assert.match(logLineMarkup({ kind: 'command', text: 'pytest -q' }), /log-command.*runs.*pytest -q/);
  assert.match(logLineMarkup({ kind: 'error', text: '<boom>', exit_code: 1 }), /error \(exit 1\).*&lt;boom&gt;/);
  assert.match(logLineMarkup({ kind: 'weird', text: 'x' }), /log-output/);
  assert.match(jobDetailMarkup({ id: 'abc123', status: 'running' }), /data-job-log="abc123"/);
});

test('approval card preview: button, starting, running link, failure', async () => {
  const { previewMarkup } = await import('./lib/markup.js');
  assert.equal(previewMarkup({ id: 'j1', previewable: false }), '');
  assert.match(previewMarkup({ id: 'j1', previewable: true, preview: null }), /data-preview-start="j1">Preview/);
  assert.match(previewMarkup({ id: 'j1', previewable: true, preview: { state: 'starting' } }), /Starting preview/);
  assert.match(previewMarkup({ id: 'j1', previewable: true, preview: { state: 'running', port: 8200 } }, '10.0.0.59'),
    /href="http:\/\/10.0.0.59:8200\/"[^>]*>Open preview/);
  assert.match(previewMarkup({ id: 'j1', previewable: true, preview: { state: 'setup_failed', error: '<npm>' } }), /&lt;npm&gt;.*Retry preview/);
});
