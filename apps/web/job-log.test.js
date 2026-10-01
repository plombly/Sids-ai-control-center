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
