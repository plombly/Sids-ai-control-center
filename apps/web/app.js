export const ENDPOINTS = ['status','repository','queue','orchestrators','workers','heartbeat','goals','jobs','approvals','failures'];
const POLL_MS = 2000;
const initial = () => Object.fromEntries(ENDPOINTS.map(key => [key, {data:null,error:null,stale:false}]));
export const state = { ...initial(), lastUpdated:null, polling:false };

const asObject = value => value && typeof value === 'object' && !Array.isArray(value) ? value : null;
const asArray = value => Array.isArray(value) ? value.filter(item => asObject(item)) : [];
const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const text = (value, fallback='—') => value === null || value === undefined || value === '' ? fallback : String(value);
export const normalize = (key, value) => {
  if (key === 'repository' || key === 'queue') return asObject(value) || {};
  if (key === 'status' || key === 'heartbeat') return asObject(value) || {};
  return asArray(value);
};

export async function fetchEndpoint(key, fetchImpl=fetch) {
  const response = await fetchImpl(`/api/${key}`, {headers:{accept:'application/json'}});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  let body; try { body = await response.json(); } catch { throw new Error('Malformed JSON'); }
  const collection = !['status','repository','queue','heartbeat'].includes(key);
  if ((collection && !Array.isArray(body)) || (!collection && (!body || typeof body !== 'object' || Array.isArray(body)))) throw new Error('Malformed payload');
  return normalize(key, body);
}

export async function poll(fetchImpl=fetch) {
  const results = await Promise.allSettled(ENDPOINTS.map(key => fetchEndpoint(key, fetchImpl)));
  results.forEach((result, index) => {
    const key = ENDPOINTS[index];
    if (result.status === 'fulfilled') state[key] = {data:result.value,error:null,stale:false};
    else state[key] = {...state[key], error:result.reason?.message || 'Request failed', stale:state[key].data !== null};
  });
  state.lastUpdated = new Date();
  render();
  return state;
}

const esc = value => text(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const number = value => finite(value) === null ? '—' : Number(value).toLocaleString();
const duration = value => finite(value) === null ? '—' : value < 60 ? `${Math.round(value)}s` : `${Math.floor(value/60)}m ${Math.round(value%60)}s`;
const statusClass = value => /fail|error|offline/i.test(text(value,'')) ? 'bad' : /wait|review|pending|idle/i.test(text(value,'')) ? 'warn' : /complete|success|active|running|online|healthy/i.test(text(value,'')) ? 'ok' : '';
const pill = value => `<span class="pill ${statusClass(value)}">${esc(value)}</span>`;
const list = (id, items, template, empty) => { const node=document.getElementById(id); node.innerHTML=items.length ? items.map(template).join('') : `<div class="empty">${empty}</div>`; };

export function render() {
  const get = key => state[key].data;
  const status = asObject(get('status')) || {}; const repo=asObject(get('repository')) || {}; const queue=asObject(get('queue')) || {};
  const workers=asArray(get('workers')); const orchestrators=asArray(get('orchestrators')); const goals=asArray(get('goals')); const jobs=asArray(get('jobs')); const approvals=asArray(get('approvals')); const failures=asArray(get('failures'));
  const statusSignals=['repository','queue','orchestrators','workers','heartbeat','goals','jobs','approvals','failures'].filter(key => status[key] !== undefined);
  const statusText=status.health||status.state||status.overall||(statusSignals.length ? (statusSignals.some(key => /fail|error|offline/i.test(text(status[key]))) ? 'Degraded' : 'Operational') : 'Awaiting telemetry');
  document.getElementById('metric-status').textContent=text(statusText); document.getElementById('metric-services').textContent=statusSignals.length ? `${statusSignals.length} status signals reporting` : 'Awaiting telemetry';
  document.getElementById('metric-branch').textContent=text(repo.branch); document.getElementById('metric-repo').innerHTML=pill(repo.status);
  document.getElementById('metric-queue').textContent=number(queue.depth); document.getElementById('metric-queue-name').textContent=text(queue.name,'Queue unavailable');
  document.getElementById('metric-workers').textContent=number(workers.filter(w=>/active|running|busy/i.test(w.status||'')).length || workers.length); document.getElementById('metric-orchestrators').textContent=`Orchestrators ${orchestrators.length}`;
  document.getElementById('orchestrator-count').textContent=orchestrators.length; document.getElementById('approval-count').textContent=approvals.length; document.getElementById('failure-count').textContent=failures.length;
  list('orchestrators',orchestrators,o=>`<div class="entity"><div class="entity-head"><span class="entity-name">${esc(o.id)}</span>${pill(o.status)}</div><div class="subtle">${esc(o.model||'Provider model unknown')}</div><div class="stats"><span>goal <b>${esc(o.active_goal||'none')}</b></span><span>heartbeat <b>${esc(o.heartbeat_age == null ? '—' : `${o.heartbeat_age}s ago`)}</b></span></div></div>`,'No orchestrators reporting');
  list('workers',workers,w=>`<div class="entity"><div class="entity-head"><span class="entity-name">${esc(w.id)}</span>${pill(w.status)}</div><div class="subtle">${esc(w.provider||'Provider unknown')} · ${esc(w.model||'model unknown')}</div><div class="stats"><span>effective <b>${number(w.effective_tokens)}</b></span><span>job <b>${esc(w.job_id||'none')}</b></span></div></div>`,'No workers reporting');
  list('goals',goals,g=>{const p=g.progress||{};const percent=p.total ? Math.min(100,Math.round((p.completed||0)/p.total*100)):0;return `<div class="item"><div class="item-head"><span class="item-title">${esc(g.summary||g.prompt||g.id)}</span>${pill(g.status)}</div><div class="subtle">${esc(g.id)} · ${p.completed||0}/${p.total||0} jobs complete</div><div class="bar"><i style="width:${percent}%"></i></div></div>`},'No goals found');
  list('approvals',approvals,j=>`<div class="item"><div class="item-head"><span class="item-title">${esc(j.id)}</span>${pill('awaiting review')}</div><p>${esc(j.role||j.provider||'Job')} · review ${esc(j.review_verdict||'pass')}</p></div>`,'No approvals waiting');
  list('failures',failures,j=>`<div class="item"><div class="item-head"><span class="item-title">${esc(j.id)}</span>${pill(j.status)}</div><p>${esc(j.error||'Failure reason unavailable')}</p></div>`,'No recent failures');
  const jobNode=document.getElementById('jobs'); jobNode.innerHTML=jobs.length?`<table class="job-table"><thead><tr><th>Job</th><th>Status</th><th>Review</th><th>Effective tokens</th><th>Duration</th></tr></thead><tbody>${jobs.map(j=>`<tr><td>${esc(j.id)}</td><td>${pill(j.status)}</td><td>${esc(j.review_status||'—')}</td><td>${number(j.effective_tokens)}</td><td>${duration(j.duration)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No jobs found</div>';
  const errors=ENDPOINTS.filter(key=>state[key].error); const banner=document.getElementById('banner'); banner.hidden=!errors.length; banner.textContent=errors.length ? `Partial telemetry: ${errors.map(key=>`${key} (${state[key].error})`).join(' · ')}. Showing the last known data where available.` : '';
  document.getElementById('live-dot').classList.toggle('offline',errors.length===ENDPOINTS.length); document.getElementById('last-updated').textContent=state.lastUpdated ? `Updated ${state.lastUpdated.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}` : 'Connecting…'; document.getElementById('poll-state').textContent=errors.length ? `${errors.length} endpoint${errors.length===1?'':'s'} degraded` : 'All endpoints healthy';
}

if (typeof document !== 'undefined') { render(); poll(); setInterval(() => poll(), POLL_MS); }
