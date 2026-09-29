import assert from 'node:assert/strict';
import {ENDPOINTS, fetchEndpoint, normalize} from './app.js';

async function main() {
  assert.deepEqual(normalize('workers', {unexpected:true}), []);
  assert.deepEqual(normalize('workers', [null, {id:'w-1'}, {progress:null}, 'bad']), [{id:'w-1'}, {progress:null}]);
  assert.deepEqual(normalize('queue', null), {});
  assert.equal(ENDPOINTS.length, 10);
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:false,status:503})), /HTTP 503/);
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:true,json:async()=>{throw new Error('bad')}})), /Malformed JSON/);
  await assert.rejects(fetchEndpoint('jobs', async () => ({ok:true,json:async()=>({})})), /Malformed payload/);
  const result = await fetchEndpoint('jobs', async () => ({ok:true,json:async()=>[{id:'j-1'}]}));
  assert.deepEqual(result, [{id:'j-1'}]);
  console.log('frontend API normalization tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
