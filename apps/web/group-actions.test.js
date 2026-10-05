import test from 'node:test';
import assert from 'node:assert/strict';
import { approveAllMarkup, buildAllMarkup, groupToggleMarkup, readyByGoal } from './lib/group-actions.js';
import { activityMarkup } from './lib/projects.js';
import { needsYouMarkup } from './lib/home.js';

const A = 'a'.repeat(40);
const B = 'b'.repeat(40);
const job = (id, goal, candidate, extra = {}) => ({ id, goal_id: goal, title: `Change ${id}`, status: 'awaiting_review', review_verdict: 'pass', project_id: 'shop', integrated_candidate_commit: candidate, ...extra });

test('approve all appears only for goals with two or more ready changes, with the exact candidates', () => {
  const ready = [job('j1', 'g1', A), job('j2', 'g1', B), job('j3', 'g2', A), job('j4', 'g3', 'short')];
  assert.deepEqual(readyByGoal(ready).map(([goal, jobs]) => [goal, jobs.length]), [['g1', 2]]);
  const html = approveAllMarkup(ready);
  assert.match(html, /data-approve-all="g1"/);
  assert.match(html, /Approve all 2/);
  const candidates = JSON.parse(html.match(/data-candidates="([^"]+)"/)[1].replaceAll('&quot;', '"'));
  assert.deepEqual(candidates, { j1: A, j2: B });
  assert.match(html, /Change j1 <code>aaaaaaaa<\/code>/);
  assert.match(needsYouMarkup({ ready, stuck: [] }), /approve-all-card/);
  assert.doesNotMatch(needsYouMarkup({ ready: [job('j1', 'g1', A)], stuck: [] }), /approve-all-card/);
});

test('group toggle, group activity and build all only for projects in a group', () => {
  const alone = { id: 'blog', children: [] };
  const parent = { id: 'shop', children: [{ id: 'shop-api' }] };
  assert.equal(groupToggleMarkup(alone, false), '');
  assert.match(groupToggleMarkup(parent, true), /data-activity-group="1" class="active"/);
  assert.equal(buildAllMarkup(alone, false), '');
  assert.equal(buildAllMarkup(parent, true), '');
  assert.match(buildAllMarkup({ id: 'shop-api', parent: 'shop' }, false), /data-build-all="shop-api"/);
  const events = [{ at: 1, kind: 'merged', title: 'Approved', project_name: 'Shop API' }];
  assert.match(activityMarkup(events, 100, parent, true), /<span class="chip">Shop API<\/span> Approved/);
  assert.doesNotMatch(activityMarkup(events, 100, parent, false), /class="chip"/);
});

test('a parent project offers approving every ready change of its group', async () => {
  const { approveGroupMarkup } = await import('./lib/group-actions.js');
  const sha = 'a'.repeat(40);
  const ready = [
    { id: 'j1', goal_id: 'g1', project_id: 'shop', title: 'Cart', integrated_candidate_commit: sha },
    { id: 'j2', goal_id: 'g2', project_id: 'shop-api', title: 'Orders', integrated_candidate_commit: sha },
    { id: 'j3', goal_id: 'g2', project_id: 'shop-api', title: 'Queued', integrated_candidate_commit: sha, merge_queue_state: 'queued' }
  ];
  const html = approveGroupMarkup({ id: 'shop', name: 'Shop', children: [{ id: 'shop-api' }] }, ready);
  assert.match(html, /2 changes across the group/);
  assert.match(html, /data-approve-group="shop"/);
  assert.doesNotMatch(html, /Queued/);
  assert.equal(approveGroupMarkup({ id: 'solo' }, ready), '');                         // not a parent
  assert.equal(approveGroupMarkup({ id: 'shop', children: [{}] }, ready.slice(1, 2)), ''); // one change
});

test('changes just approved are not offered again until the merge queue reports them', async () => {
  const { approveGroupMarkup, approveAllMarkup, markQueued, recentlyQueued } = await import('./lib/group-actions.js');
  const sha = 'b'.repeat(40);
  const ready = [
    { id: 'q1', goal_id: 'g1', project_id: 'shop', integrated_candidate_commit: sha },
    { id: 'q2', goal_id: 'g2', project_id: 'shop-api', integrated_candidate_commit: sha },
    { id: 'q3', goal_id: 'g1', project_id: 'shop', integrated_candidate_commit: sha }
  ];
  const parent = { id: 'shop', children: [{ id: 'shop-api' }] };
  assert.match(approveGroupMarkup(parent, ready), /3 changes/);
  markQueued(['q1', 'q2', 'q3'], Date.now());
  assert.equal(approveGroupMarkup(parent, ready), '');
  assert.equal(approveAllMarkup(ready), '');
  assert.equal(recentlyQueued({ id: 'q1', merge_queue_state: 'queued' }), false);   // the server knows now
  assert.equal(recentlyQueued({ id: 'q2' }, Date.now() + 200000), false);           // gave up waiting
});
