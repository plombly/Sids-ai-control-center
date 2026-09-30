import { asArray, asObject } from './format.js';

export const ENDPOINTS = [
  'status',
  'repository',
  'queue',
  'orchestrators',
  'workers',
  'heartbeat',
  'goals',
  'jobs',
  'approvals',
  'failures'
];
export const HISTORY_PAGE_SIZE = 25;
export const normalize = (key, value) =>
  ['repository', 'queue', 'status', 'heartbeat'].includes(key) ? asObject(value) || {} : asArray(value);
export async function fetchEndpoint(key, fetchImpl = fetch) {
  const path = ['goals', 'jobs', 'approvals', 'failures'].includes(key) ? `/api/${key}?limit=100` : `/api/${key}`;
  const response = await fetchImpl(path, { headers: { accept: 'application/json' } });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error('Malformed JSON');
  }
  const collection = !['status', 'repository', 'queue', 'heartbeat'].includes(key);
  if (
    (collection && !Array.isArray(body)) ||
    (!collection && (!body || typeof body !== 'object' || Array.isArray(body)))
  )
    throw new Error('Malformed payload');
  return normalize(key, body);
}
export async function fetchHistoryPage(offset, fetchImpl = fetch) {
  const response = await fetchImpl(`/api/jobs?limit=${HISTORY_PAGE_SIZE}&offset=${offset}`, {
    headers: { accept: 'application/json' }
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error('Malformed JSON');
  }
  if (!Array.isArray(body)) throw new Error('Malformed payload');
  return normalize('jobs', body);
}
// Operator token for writes (the API requires it when SID_OPERATOR_TOKEN is
// set). Kept in this browser only; storage can be unavailable.
const TOKEN_KEY = 'sid-operator-token';
export const operatorToken = {
  get() {
    try {
      return globalThis.localStorage?.getItem(TOKEN_KEY) || '';
    } catch {
      return '';
    }
  },
  set(value) {
    try {
      if (value) globalThis.localStorage?.setItem(TOKEN_KEY, value);
      else globalThis.localStorage?.removeItem(TOKEN_KEY);
    } catch {}
  }
};
export const authHeaders = (token = operatorToken.get()) => (token ? { 'x-sid-token': token } : {});
export async function requestJSON(path, options = {}, fetchImpl = fetch) {
  const response = await fetchImpl(path, {
    ...options,
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      ...authHeaders(),
      ...(options.headers || {})
    }
  });
  let body = {};
  try {
    body = await response.json();
  } catch {}
  if (!response.ok) throw new Error(body.detail || body.message || `HTTP ${response.status}`);
  return body;
}
export const fetchJobDetail = (jobId, fetchImpl = fetch) =>
  requestJSON(`/api/jobs/${encodeURIComponent(jobId)}`, {}, fetchImpl);
export const submitGoal = (goal, atomic = false, request_id = '', fetchImpl = fetch) =>
  requestJSON(
    '/api/prompts',
    { method: 'POST', body: JSON.stringify({ prompt: goal, atomic, request_id: request_id || undefined }) },
    fetchImpl
  );
export const workerAction = (id, action, fetchImpl = fetch) =>
  requestJSON(`/api/workers/${encodeURIComponent(id)}/${action}`, { method: 'POST' }, fetchImpl);
export const removeWorker = (id, fetchImpl = fetch) =>
  requestJSON(`/api/workers/${encodeURIComponent(id)}`, { method: 'DELETE' }, fetchImpl);
export const jobAction = (jobId, body, fetchImpl = fetch) =>
  requestJSON(
    `/api/jobs/${encodeURIComponent(jobId)}/actions`,
    { method: 'POST', body: JSON.stringify(body) },
    fetchImpl
  );
export const operatorRequest = (requestId, fetchImpl = fetch) =>
  requestJSON(`/api/operator-requests/${encodeURIComponent(requestId)}`, { method: 'GET' }, fetchImpl);
// Through requestJSON so the operator token is sent like every other write.
export const dismiss = (ids, fetchImpl = fetch) =>
  requestJSON('/api/dismissals', { method: 'POST', body: JSON.stringify({ ids }) }, fetchImpl);
export async function loadDismissals(fetchImpl = fetch) {
  const response = await fetchImpl('/api/dismissals');
  if (!response.ok) throw new Error(`Unable to load dismissals (HTTP ${response.status})`);
  const body = await response.json();
  return body.ids;
}
export async function handoffText(goalId, fetchImpl = fetch) {
  const response = await fetchImpl(`/api/goals/${encodeURIComponent(goalId)}/handoff-data`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const bundle = await response.json();
  return JSON.stringify(bundle, null, 2);
}
