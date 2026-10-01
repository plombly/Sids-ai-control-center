import test from 'node:test';
import assert from 'node:assert/strict';
import { timeAgo, statusMarkup, needsYou, needsYouMarkup, inProgressMarkup, projectsMarkup, recentMarkup, composerMarkup } from './lib/home.js';

const NOW = 1_800_000_000;

test('time ago in plain words', () => {
  assert.equal(timeAgo(NOW - 30, NOW), 'just now');
  assert.equal(timeAgo(NOW - 600, NOW), '10 min ago');
  assert.equal(timeAgo(NOW - 7200, NOW), '2 h ago');
  assert.equal(timeAgo(NOW - 3 * 86400, NOW), '3 d ago');
  assert.equal(timeAgo(0, NOW), '');
});

test('status line: calm when fine, names the problem when not', () => {
  const ok = statusMarkup({ health: { report: { status: 'ok', checks: [{ name: 'disk', level: 'ok' }] } }, workers: [{ status: 'idle' }, { status: 'working' }], jobs: [{ status: 'queued' }], projects: [{}, {}] });
  assert.match(ok, /Everything is running normally/);
  assert.match(ok, /2 workers · 1 busy/);
  assert.match(ok, /1 queued/);
  const bad = statusMarkup({ health: { report: { status: 'fail', checks: [{ name: 'disk', level: 'fail' }, { name: 'backup', level: 'warn' }] } } });
  assert.match(bad, /level-fail/);
  assert.match(bad, /2 things need a look: disk, backup/);
});

test('needs you: ready approvals and stuck jobs with plain actions', () => {
  const items = needsYou({
    approvals: [{ id: 'a1', status: 'awaiting_review', review_verdict: 'pass', title: 'Add <pricing> page', project_id: 'shop', integrated_candidate_commit: 'c1', previewable: true }],
    jobs: [{ id: 'n1', status: 'needs_human', title: 'Fix login', project_id: 'shop' }, { id: 'n2', status: 'needs_human' }],
    dismissed: new Set(['n2'])
  });
  assert.equal(items.ready.length, 1);
  assert.equal(items.stuck.length, 1);
  const html = needsYouMarkup(items, { shop: 'Shop' });
  assert.match(html, /Add &lt;pricing&gt; page/);
  assert.match(html, /data-op="approve" data-job="a1" data-status="awaiting_review" data-candidate="c1"/);
  assert.match(html, /data-preview-start="a1"/);
  assert.match(html, /data-op="extend" data-job="n1" data-status="needs_human">Try again/);
  assert.match(html, /<span class="chip">Shop<\/span>/);
  assert.match(needsYouMarkup({ ready: [], stuck: [] }), /Nothing needs you/);
});

test('in progress: progress bar and what is happening now', () => {
  const html = inProgressMarkup({
    goals: [{ id: 'g1', status: 'running', prompt: 'Build the shop\nmore detail', project_id: 'shop', progress: { completed: 1, total: 4 }, created_at: NOW - 120 }, { id: 'g2', status: 'completed' }],
    jobs: [{ id: 'j1', goal_id: 'g1', status: 'running', role: 'builder', title: 'Add cart' }]
  }, {}, NOW);
  assert.match(html, /Build the shop</);
  assert.match(html, /width:25%/);
  assert.match(html, /1 of 4 steps done · Building: Add cart/);
  assert.doesNotMatch(html, /g2/);
  assert.match(inProgressMarkup({ goals: [] }), /SID is idle/);
});

test('projects and recent work', () => {
  const html = projectsMarkup([{ id: 'shop', name: 'Shop', importance: 'high', counts: { jobs_running: 1, jobs_awaiting_approval: 2 }, app: { state: 'running', port: 8100 } }], '10.0.0.59');
  assert.match(html, /href="#\/projects\/shop"/);
  assert.match(html, /1 job in progress · 2 to approve/);
  assert.match(html, /href="http:\/\/10.0.0.59:8100\/"/);
  assert.match(html, /\+ New project/);
  assert.doesNotMatch(html, /<a [^>]*>(?:(?!<\/a>).)*<a /s);  // no link inside a link
  const recent = recentMarkup([
    { id: 'g1', status: 'completed', prompt: 'Old', updated_at: NOW - 9000 },
    { id: 'g2', status: 'failed', prompt: 'New', updated_at: NOW - 60, project_id: 'shop' },
    { id: 'g3', status: 'running', prompt: 'Not yet' }
  ], {}, NOW);
  assert.ok(recent.indexOf('New') < recent.indexOf('Old'));
  assert.match(recent, /mark bad/);
  assert.doesNotMatch(recent, /Not yet/);
});

test('composer lists projects', () => {
  const html = composerMarkup([{ id: 'sid', name: 'SID' }, { id: 'shop', name: '<Shop>' }]);
  assert.match(html, /id="assist-home"/);
  assert.match(html, /<option value="shop">&lt;Shop&gt;<\/option>/);
});

test('a job asking for internet access gets its own card', () => {
  const html = needsYouMarkup({ ready: [], stuck: [{ id: 'j1', status: 'needs_human', needs_human_kind: 'network', network_request_step: 'tests', network_request_reason: 'getaddrinfo <EAI_AGAIN>', title: 'Add Stripe' }] }, {});
  assert.match(html, /Wants internet/);
  assert.match(html, /getaddrinfo &lt;EAI_AGAIN&gt;/);
  for (const op of ['network_once', 'network_always', 'network_deny']) assert.match(html, new RegExp(`data-op="${op}" data-job="j1" data-status="needs_human"`));
  assert.doesNotMatch(html, /data-op="extend"/);
  const stuck = needsYouMarkup({ ready: [], stuck: [{ id: 'j2', status: 'needs_human', needs_human_kind: 'build' }] }, {});
  assert.match(stuck, /data-op="extend"/);
});
