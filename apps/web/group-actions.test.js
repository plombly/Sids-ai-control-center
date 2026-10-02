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
