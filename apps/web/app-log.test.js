import test from 'node:test';
import assert from 'node:assert/strict';
import { appLogMarkup, bodyText, metaText } from './lib/app-log.js';

test('live log: closed by default, escaped text, restart and exit facts', () => {
  assert.match(appLogMarkup('game'), /<details class="app-log-box" data-app-log="game">/);
  assert.equal(bodyText({ lines: ['<b>hi</b>', 'second'] }), '<b>hi</b>\nsecond');
  assert.equal(bodyText({ lines: [] }), 'Nothing logged yet.');
  assert.equal(metaText({ since: 'Sun 2026-10-04 09:30:00 UTC', restarts: 2, result: 'exit-code', lines: [] }),
    'running since Sun 2026-10-04 09:30:00 UTC · restarted 2 times · last exit: exit-code');
  assert.equal(metaText({ restarts: 0, result: 'success', lines: [] }), 'no restarts');
  assert.equal(metaText(null), 'Loading…');
});
