// Settings → AI, "Routing": which AI did which work in the last 7 days, every
// switch to the other provider and why, waits that used no attempt, and the
// worker scaler's recent decisions (apps/api/routing_routes.py).
import { esc } from './format.js';

const when = epoch => {
  const date = new Date(Number(epoch) * 1000);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
};

export function routingMarkup(data = {}) {
  const roles = Array.isArray(data.roles) ? data.roles : [];
  const rows = roles
    .map(role => `<tr><td>${esc(role.role)}</td><td>${(role.runs || []).map(run => `${esc(run.provider)}${run.model ? ` <span class="subtle">${esc(run.model)}</span>` : ''} × ${esc(run.count)}`).join('<br>')}</td></tr>`)
    .join('');
  const fallbacks = (data.fallbacks || []).slice(0, 10)
    .map(item => `<li><span class="subtle">${esc(when(item.at))}</span> ${esc(item.role)} <code>${esc(item.job)}</code>: ${esc(item.reason)}</li>`)
    .join('');
  const waits = data.waits || {};
  const waitText = [waits.provider_waits ? `${waits.provider_waits} wait(s) for a paused provider` : '', waits.interruptions ? `${waits.interruptions} restart(s) survived` : '']
    .filter(Boolean).join(' · ');
  const scaler = (data.scaler || []).slice(0, 10)
    .map(item => `<li><span class="subtle">${esc(when(item.at))}</span> ${esc(item.reason || item.action || '')}${item.target != null ? ` → ${esc(item.target)} worker(s)` : ''}</li>`)
    .join('');
  return `<div class="settings-card"><h3>Routing (last ${esc(data.days || 7)} days)</h3>${rows ? `<div class="table-wrap"><table class="job-table"><thead><tr><th>Role</th><th>Ran on</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="subtle">No runs yet.</p>'}<h4>Switched provider</h4>${fallbacks ? `<ul class="plain-list">${fallbacks}</ul>` : '<p class="subtle">Never: every run used its usual provider.</p>'}${waitText ? `<p class="subtle">Without using attempts: ${esc(waitText)}.</p>` : ''}<h4>Worker scaling</h4>${scaler ? `<ul class="plain-list">${scaler}</ul>` : '<p class="subtle">No recent decisions.</p>'}</div>`;
}
