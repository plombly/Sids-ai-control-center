import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
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
  approvalMarkup
} from './app.js';

async function main() {
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
  const appSource = fs.readFileSync(new URL('./app.js', import.meta.url), 'utf8');
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
