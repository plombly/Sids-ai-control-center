import test from 'node:test';
import assert from 'node:assert/strict';
import { mergeQueueMarkup, queueControlsMarkup } from './lib/merge-queue.js';
import { pill } from './lib/format.js';

test('mergeQueueMarkup renders and escapes queue items', () => {
  const markup = mergeQueueMarkup({
    items: [{
      position: 1,
      id: 'abc123',
      title: '<b>x</b>',
      status: 'merge_queued',
      state: 'queued',
      reason: '<b>x</b>',
      fresh: true
    }]
  });

  assert.match(markup, /1/);
  assert.match(markup, /data-detail="abc123"/);
  assert.match(markup, /data-dequeue="abc123"/);
  assert.match(markup, /data-status="merge_queued"/);
  assert.match(markup, /class="pill[^"]*">queued<\/span>/);
  assert.equal(markup.includes(pill('queued')), true);
  assert.match(markup, /yes/);
  assert.match(markup, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.doesNotMatch(markup, /<b>x<\/b>/);
});

test('mergeQueueMarkup renders no for a stale item', () => {
  assert.match(mergeQueueMarkup({ items: [{ fresh: false }] }), />no</);
});

test('mergeQueueMarkup renders the empty state', () => {
  const empty = '<div class="empty">Merge queue is empty</div>';
  assert.equal(mergeQueueMarkup({ items: [] }), empty);
  assert.equal(mergeQueueMarkup({}), empty);
  assert.equal(mergeQueueMarkup(null), empty);
});

test('queueControlsMarkup renders ready approvals only', () => {
  const markup = queueControlsMarkup([
    { id: 'one', status: 'awaiting_review', integrated_candidate_commit: 'a'.repeat(40) },
    { id: 'two', status: 'approved', integrated_candidate_commit: 'b'.repeat(40) },
    { id: 'three', status: 'awaiting_review' }
  ]);

  assert.equal((markup.match(/data-queue-approve=/g) || []).length, 2);
  assert.match(markup, /data-queue-approve="one"/);
  assert.match(markup, /data-candidate="a{40}"/);
  assert.match(markup, /data-status="awaiting_review"/);
  assert.match(markup, /data-queue-approve="two"/);
  assert.match(markup, /data-candidate="b{40}"/);
  assert.match(markup, /data-status="approved"/);
  assert.doesNotMatch(markup, /data-queue-approve="three"/);
  assert.match(markup, /Queue all ready \(2\)/);
  assert.match(markup, /data-queue-all/);
});

test('queueControlsMarkup is empty without ready approvals', () => {
  assert.equal(queueControlsMarkup([]), '');
  assert.equal(queueControlsMarkup([{ id: 'x', status: 'awaiting_review' }]), '');
  assert.equal(queueControlsMarkup(undefined), '');
});
