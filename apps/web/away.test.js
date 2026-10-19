import { test } from 'node:test';
import assert from 'node:assert/strict';
import { awayMarkup } from './lib/away.js';

test('what LAIka handled by itself, newest first, with a way to dismiss', () => {
  const html = awayMarkup([{ job: 'b1', title: 'Cart', what: 'survived a restart', at: 1000 }], 1000 + 600);
  assert.match(html, /<b>Cart<\/b>: survived a restart <span class="subtle">10 min ago/);
  assert.match(html, /data-detail="b1"/);
  assert.match(html, /data-away-seen/);
  assert.equal(awayMarkup([]), '');
});
