import assert from 'node:assert/strict';
import {ENDPOINTS, fetchEndpoint, normalize, workerMarkup} from './app.js';

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
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:false,status:503})), /HTTP 503/);
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:true,json:async()=>{throw new Error('bad')}})), /Malformed JSON/);
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:true,json:async()=>({})})), /Malformed payload/);
  const result = await fetchEndpoint('jobs', async () => ({ok:true,json:async()=>[{id:'j-1'}]}));
  assert.deepEqual(result, [{id:'j-1'}]);
  console.log('frontend API normalization tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
