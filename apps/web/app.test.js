import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  authHeaders,
  operatorToken,
  requestJSON,
  tokenStateText,
  ENDPOINTS,
  fetchEndpoint,
  fetchHistoryPage,
  normalize,
  workerMarkup,
  submitGoal,
  workerAction,
  jobAction,
  operatorRequest,
  jobActionsMarkup,
  approvalMarkup,
  fetchJobDetail,
  jobDetailMarkup,
  dismiss,
  loadDismissals
} from './app.js';

async function main() {
  // Operator token: sent on requests when set, absent otherwise.
  assert.deepEqual(authHeaders(''), {});
  assert.deepEqual(authHeaders('tok'), {'x-sid-token': 'tok'});
  assert.equal(operatorToken.get(), '');  // no localStorage under node: must not throw
  const seen = [];
  await requestJSON('/api/x', {method: 'POST', headers: {'x-sid-token': 'tok'}},
    async (path, opts) => { seen.push(opts); return {ok: true, json: async () => ({})}; });
  assert.equal(seen[0].headers['x-sid-token'], 'tok');
  assert.equal(seen[0].headers['content-type'], 'application/json');
  assert.equal(seen[0].method, 'POST');
  assert.match(tokenStateText({token_required: false}), /open/);
  assert.equal(tokenStateText({token_required: true, token_valid: true}), 'Actions enabled');
  assert.match(tokenStateText({token_required: true, token_valid: false}), /needed/);
  assert.deepEqual(normalize('workers', {unexpected:true}), []);
  assert.deepEqual(normalize('workers', [null, {id:'w-1'}, {progress:null}, 'bad']), [{id:'w-1'}, {progress:null}]);
  assert.deepEqual(normalize('queue', null), {});
  assert.equal(ENDPOINTS.length, 10);
  const worker = workerMarkup({
    id:'w-1', status:'active', role:'builder', provider:'openai', model:'gpt-5', job_id:'j-1',
    effective_tokens:1200, cached_input_tokens:300, command_count:4, duration:65, heartbeat_age:12,
  });
  assert.match(worker, /builder/);
  assert.match(worker, /job <b>j-1<\/b>/);
  assert.match(worker, /cached <b>300<\/b>/);
  assert.match(worker, /commands <b>4<\/b>/);
  assert.match(worker, /duration <b>1m 5s<\/b>/);
  assert.match(worker, /heartbeat <b>12s ago<\/b>/);
  const runningWorker = workerMarkup({id:'w-3', status:'running', job_id:'j-3', job_started_at:100}, 160);
  assert.match(runningWorker, /running <span class="elapsed" data-elapsed-since="100">1m 00s<\/span> on job j-3 · started/);
  assert.match(workerMarkup({id:'w-2', status:'idle'}), /Provider unknown/);
  assert.match(workerMarkup({id:'w-2', status:'idle'}), /cached <b>—<\/b>/);
  assert.match(workerMarkup({id:'w-2', status:'idle'}), /heartbeat <b>—<\/b>/);
  assert.match(workerMarkup({id:'<worker>', status:'active', role:'<role>'}), /&lt;worker&gt;/);
  assert.doesNotMatch(workerMarkup({id:'<worker>', status:'active', role:'<role>'}), /<worker>/);
  const calls = [];
  const write = async (path, options) => { calls.push([path, options]); return {ok:true, json:async()=>({id:'g-1'})}; };
  assert.deepEqual(await submitGoal('ship it', true, 'req-1', write), {id:'g-1'});
  await workerAction('busy/worker', 'stop', write);
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[1][0], '/api/workers/busy%2Fworker/stop');
  await jobAction('job/1', {action:'reject', request_id:'req-1'}, write);
  assert.equal(calls[2][0], '/api/jobs/job%2F1/actions');
  assert.equal(calls[2][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[2][1].body), {action:'reject', request_id:'req-1'});
  await operatorRequest('req/1', write);
  assert.equal(calls[3][0], '/api/operator-requests/req%2F1');
  assert.equal(calls[3][1].method, 'GET');
  assert.match(approvalMarkup({id:'job-1', status:'awaiting_review', review_status:'complete', review_verdict:'pass', integrated_candidate_commit:'0123456789abcdef'}), /job-review.py approve job-1/);
  assert.match(approvalMarkup({id:'job-1', status:'awaiting_review', integrated_candidate_commit:'0123456789abcdef'}), /data-op="approve"/);
  assert.match(approvalMarkup({id:'job-1', status:'awaiting_review', integrated_candidate_commit:'0123456789abcdef'}), /data-candidate="0123456789abcdef"/);
  assert.doesNotMatch(approvalMarkup({id:'job-1', status:'awaiting_review'}), /data-op="approve"/);
  assert.doesNotMatch(approvalMarkup({id:'job-1', status:'awaiting_review'}), /Approval controls are unavailable/);
  assert.match(jobActionsMarkup({id:'job-1', status:'awaiting_review'}), /Reject/);
  assert.match(jobActionsMarkup({id:'job-1', status:'awaiting_review'}), /Reintegrate/);
  assert.match(jobActionsMarkup({id:'job-1', status:'needs_human'}), /Reject/);
  assert.match(jobActionsMarkup({id:'job-1', status:'needs_human'}), /Extend \(\+1\)/);
  assert.match(jobActionsMarkup({id:'job-1', status:'needs_human'}), /Reintegrate/);
  assert.match(jobActionsMarkup({id:'job-1', status:'blocked_failed_dependency'}), /Reopen/);
  assert.equal(jobActionsMarkup({id:'job-1', status:'merged'}), '');
  const escapedActions = jobActionsMarkup({id:'<job>', status:'needs_human'});
  assert.match(escapedActions, /data-job="&lt;job&gt;"/);
  assert.doesNotMatch(escapedActions, /data-job="<job>"/);
  const endpointPaths = [];
  const endpointFetch = async path => {
    endpointPaths.push(path);
    return {ok:true,json:async()=> path === '/api/status' ? {} : [{id:'j-1'}]};
  };
  await fetchEndpoint('jobs', endpointFetch);
  await fetchEndpoint('status', endpointFetch);
  assert.equal(endpointPaths[0], '/api/jobs?limit=100');
  assert.equal(endpointPaths[1], '/api/status');
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:false,status:503})), /HTTP 503/);
  const dismissalCalls = [];
  const dismissalFetch = async (path, options = {}) => {
    dismissalCalls.push([path, options]);
    return {ok:true, json:async()=>({ids:['j-1']})};
  };
  assert.deepEqual(await dismiss(['j-1'], dismissalFetch), {ids:['j-1']});
  assert.equal(dismissalCalls[0][0], '/api/dismissals');
  assert.equal(dismissalCalls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(dismissalCalls[0][1].body), {ids:['j-1']});
  assert.equal(dismissalCalls[0][1].headers['content-type'], 'application/json');  // shared write path (sends the operator token)
  assert.deepEqual(await loadDismissals(dismissalFetch), ['j-1']);
  assert.equal(dismissalCalls[1][1].method, undefined);
  await assert.rejects(dismiss(['j-1'], async () => ({ok:false,status:422})), /HTTP 422/);
  await assert.rejects(loadDismissals(async () => ({ok:false,status:503})), /HTTP 503/);
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:true,json:async()=>{throw new Error('bad')}})), /Malformed JSON/);
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:true,json:async()=>({})})), /Malformed payload/);
  const result = await fetchEndpoint('jobs', async () => ({ok:true,json:async()=>[{id:'j-1'}]}));
  assert.deepEqual(result, [{id:'j-1'}]);
  const historyPaths = [];
  const historyFetch = async path => {
    historyPaths.push(path);
    return {ok:true,json:async()=>[{id:'j-1'}]};
  };
  assert.deepEqual(await fetchHistoryPage(25, historyFetch), [{id:'j-1'}]);
  assert.equal(historyPaths[0], '/api/jobs?limit=25&offset=25');
  await assert.rejects(fetchHistoryPage(0, async () => ({ok:true,json:async()=>({})})), /Malformed payload/);
  const detailPaths = [];
  const detail = {id: '<job>&', status: 'complete'};
  assert.deepEqual(
    await fetchJobDetail('job/one & two', async path => {
      detailPaths.push(path);
      return {ok:true, json:async()=>detail};
    }),
    detail
  );
  assert.equal(detailPaths[0], '/api/jobs/job%2Fone%20%26%20two');
  const detailMarkup = jobDetailMarkup({
    id: '<j1>', title: '<title>', status: 'needs_human', role: 'builder', worker: 'w1',
    provider: 'openai', model: 'm&1', review_status: 'complete', review_verdict: 'pass',
    integration_status: 'ready', goal_id: 'g1', branch: 'feature', base: 'base', candidate: 'candidate',
    integration_base_commit: 'base-2', integrated_candidate_commit: 'candidate-2', reviewed_commit: 'reviewed',
    integration_worktree: 'worktree', integration_branch: 'main', duration: 12, started_at: 100, finished_at: 160, elapsed_seconds: 60, effective_tokens: 42,
    cached_input_tokens: 3, output_tokens: 4, command_count: 5, files: 6, tests: 'passed', error: null,
    repair_status: 'retry', repair_attempts: 1, max_repair_attempts: 2, needs_human_reason: '<reason>',
    review_job_id: 'review-1', review_findings: '<findings>',
    lineage: {
      build_attempt: 2, max_build_attempts: 3, review_recoveries: 1, retry_reason: 'retry',
      needs_human_kind: 'build', repair_job_id: 'repair-1', last_repair_job_id: 'repair-0',
      last_integrate_job_id: 'integrate-1', source_candidate_commits: ['zz-first', 'aa-second'],
      review_findings_history: [{review_job_id: 'old', candidate: 'c0', findings: 'old finding'}]
    },
    gate: {returncode: 1, summary: '<gate>'},
    related: [{
      id: 'related-1', role: 'reviewer', status: 'done', review_verdict: 'pass', duration: 1,
      started_at: 100, finished_at: 160, elapsed_seconds: 60, effective_tokens: 2, created_at: 3
    }]
  });
  for (const value of ['&lt;j1&gt;', '&lt;title&gt;', 'zz-first', 'aa-second', '&lt;gate&gt;', 'related-1']) {
    assert.match(detailMarkup, new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  assert.ok(detailMarkup.indexOf('zz-first') < detailMarkup.indexOf('aa-second'));
  for (const heading of ['Started', 'Finished', 'Elapsed']) assert.match(detailMarkup, new RegExp(`<b>${heading}<\/b>`));
  assert.match(detailMarkup, /<th>Started<\/th><th>Elapsed<\/th>/);
  assert.match(detailMarkup, /1m 00s/);
  const running = jobDetailMarkup({ id: 'run', started_at: 100, finished_at: null }, 112);
  assert.match(running, /<b>Elapsed<\/b><span><span class="elapsed" data-elapsed-since="100">12s<\/span><\/span>/);
  for (const heading of ['Summary', 'Candidate', 'Attempts', 'Review', 'Gate', 'Related jobs']) {
    assert.match(detailMarkup, new RegExp(`<h3>${heading}</h3>`));
  }
  assert.match(jobDetailMarkup({id:'j2'}), /No gate result/);
  assert.match(jobDetailMarkup({id:'j2'}), /<span>—<\/span>/);
  assert.doesNotMatch(jobDetailMarkup({id:'old', related:[{id:'old-related'}]}), /<b>Started<\/b>/);
  const appSource = fs.readFileSync(new URL('./app.js', import.meta.url), 'utf8');
  const indexSource = fs.readFileSync(new URL('./index.html', import.meta.url), 'utf8');
  assert.match(indexSource, /data-dismiss-all="failures"/);
  assert.match(indexSource, /data-dismiss-all="history"/);
  for (const field of ['command_count', 'j.files', 'j.base', 'j.candidate', 'j.tests', 'j.error']) {
    assert.match(appSource, new RegExp(field.replace('.', '\\.')));
  }

  console.log('frontend API normalization tests passed');

  for (const status of ['working', 'busy', 'claimed', 'running', 'active', 'stopping']) {
    const markup = workerMarkup({id:`w-${status}`, status});
    assert.match(markup, /data-action="remove" disabled/);
    assert.match(markup, /Remove \(busy\)/);
  }

  {
    const markup = workerMarkup({
      id:'w-active-job',
      status:'idle',
      active_job_id:'j-live'
    });
    assert.match(markup, /data-action="remove" disabled/);
    assert.match(markup, /Remove \(busy\)/);
  }

  for (const status of [
    'completed',
    'test_failed',
    'rejected',
    'repair_exhausted',
    'blocked_failed_dependency',
    'planning_failed'
  ]) {
    const markup = workerMarkup({id:`w-${status}`, status});
    assert.doesNotMatch(markup, /data-action="remove" disabled/);
  }

}

main().catch(error => { console.error(error); process.exitCode = 1; });

{
  const html = jobActionsMarkup({ id: 'job-1', status: 'needs_human', needs_human_kind: 'network' });
  assert.match(html, /data-op="network_once"/);
  assert.match(html, /data-op="network_deny"/);
  assert.doesNotMatch(html, /data-op="extend"/);
}
