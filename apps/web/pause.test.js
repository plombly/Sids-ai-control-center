import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pauseBannerMarkup, pauseCardMarkup } from './lib/pause.js';

test('a banner only while paused, saying who and why, with Resume', () => {
  assert.equal(pauseBannerMarkup({ paused: false }), '');
  const html = pauseBannerMarkup({ paused: true, by: 'sam', reason: 'server upgrade' });
  assert.match(html, /All new work is paused by sam: server upgrade/);
  assert.match(html, /data-pause-set="resume"/);
  assert.match(pauseCardMarkup({ paused: false }), /data-pause-set="pause">Pause all new work/);
  assert.match(pauseCardMarkup({ paused: true }), /Resume work/);
});
