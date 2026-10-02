import test from 'node:test';
import assert from 'node:assert/strict';
import { scaleMarkup } from './lib/worker-scale.js';

const base = { target: 4, running: 4, auto: true, min: 1, max: 7, fixed: 8, idle: 1, runnable: 2, projects: 2, memory_percent: 51.6, load5: 1.2, cpus: 4, hold: false };

test('scale bar: count, mode, demand, buttons', () => {
  const html = scaleMarkup({ state: base, log: [] });
  assert.match(html, /<b>4<\/b> workers/);
  assert.match(html, /Automatic, 1–7 · 2 ready to run in 2 projects · memory 52% free · load 1\.2\/4 CPUs/);
  assert.match(html, /data-scale="-1"/);
  assert.doesNotMatch(html, /data-scale="1"[^>]*disabled/);
  assert.match(scaleMarkup({ state: { ...base, target: 7, running: 7 } }), /data-scale="1"[^>]*disabled/);
  assert.match(scaleMarkup({ state: { ...base, target: 1, running: 1 } }), /data-scale="-1"[^>]*disabled/);
  assert.match(scaleMarkup({ state: { ...base, auto: false } }), /Fixed number/);
});

test('scale bar: changes in progress, hold, log, no scaler', () => {
  assert.match(scaleMarkup({ state: { ...base, target: 3 } }), /finishing jobs, then stopping → 3/);
  assert.match(scaleMarkup({ state: { ...base, target: 5 } }), /starting → 5/);
  assert.match(scaleMarkup({ state: base, hold: 'memory critical (5% free)' }), /New work paused:<\/b> memory critical \(5% free\)/);
  const log = scaleMarkup({ state: base, log: [{ at: 1790900000, target: 5, reason: 'adding a worker: <x>' }] }, 1790900100, true);
  assert.match(log, /→ 5: adding a worker: &lt;x&gt;/);
  assert.doesNotMatch(scaleMarkup({ state: base, log: [{ at: 1, target: 5, reason: 'r' }] }), /scale-log/);
  assert.match(scaleMarkup({ state: null }), /scaler not running/);
});
