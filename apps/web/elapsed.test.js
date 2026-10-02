import assert from 'node:assert/strict';
import test from 'node:test';
import { elapsedText, clockText, elapsedMarkup } from './lib/elapsed.js';

test('elapsedText formats boundaries and rejects invalid values', () => {
  const cases = [[0, '0s'], [45, '45s'], [59, '59s'], [60, '1m 00s'], [252, '4m 12s'], [3599, '59m 59s'], [3780, '1h 03m'], [86399, '23h 59m'], [187200, '2d 4h'], [null, ''], [-5, '']];
  for (const [seconds, expected] of cases) assert.equal(elapsedText(seconds), expected);
  assert.equal(elapsedText(NaN), '');
});

test('clockText uses local dates', () => {
  const same = new Date(2026, 9, 1, 14, 3);
  assert.equal(clockText(same.getTime() / 1000, same.getTime() / 1000), '14:03');
  const other = new Date(2026, 8, 30, 14, 3);
  assert.equal(clockText(other.getTime() / 1000, same.getTime() / 1000), 'Sep 30 14:03');
});

test('elapsedMarkup covers missing, running and finished jobs', () => {
  assert.equal(elapsedMarkup(null, null, 100), '');
  assert.equal(elapsedMarkup(40, null, 100), '<span class="elapsed" data-elapsed-since="40">1m 00s</span>');
  assert.equal(elapsedMarkup(40, 100, 120), `took 1m 00s · finished ${clockText(100, 120)}`);
});

test('importing elapsed in node does not start a timer', () => {
  assert.equal(typeof window, 'undefined');
  assert.equal(typeof document, 'undefined');
});
