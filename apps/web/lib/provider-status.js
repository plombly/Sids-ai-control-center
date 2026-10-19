// Settings → AI: whether each AI provider is taking work right now, until
// when it is paused and why (services/provider_health.py), and Claude's own
// plan-usage report (5-hour and 7-day windows) from its latest run.
import { esc, escValue } from './format.js';

const KIND = {
  quota: 'Usage limit reached',
  rate: 'Too many requests',
  auth: 'Needs signing in again',
  unavailable: 'Not reachable'
};

const clock = epoch => {
  const date = new Date(Number(epoch) * 1000);
  if (Number.isNaN(date.getTime())) return '';
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
};

export function holdMarkup(name, hold) {
  if (!hold?.held) return `<li><b>${esc(name)}</b> <span class="pill ok">Taking work</span></li>`;
  const kind = KIND[hold.kind] || 'Paused';
  return `<li><b>${esc(name)}</b> <span class="pill warn">${esc(kind)}</span> <span class="subtle">paused until ${esc(clock(hold.until))}</span><div class="subtle">${esc(hold.reason || '')}</div></li>`;
}

export function usageMarkup(reading, name = 'Claude') {
  const windows = reading?.windows || {};
  const labels = { five_hour: '5-hour window', seven_day: '7-day window' };
  const rows = Object.entries(windows)
    .filter(([, w]) => typeof w?.percent === 'number')
    .map(([name, w]) => {
      const pct = Math.max(0, Math.min(100, w.percent));
      const state = pct >= 95 ? 'over' : pct >= 80 ? 'warn' : 'ok';
      return `<div class="spend ${escValue(state)}"><span>${esc(labels[name] || name.replace('_', ' '))}: ${esc(pct)}% used${w.resets_at ? ` · resets ${esc(clock(w.resets_at))}` : ''}</span><div class="progress"><i style="width:${pct}%"></i></div></div>`;
    })
    .join('');
  if (!rows) return '';
  const when = reading.at ? ` (reported ${esc(clock(reading.at))})` : '';
  return `<h4>${esc(name)} plan usage${when}</h4>${rows}<p class="field-hint">From ${esc(name)}'s own report in each run (the first sign-in). At 95% LAIka stops sending it work until the window resets (setting CLAUDE_EARLY_HOLD_PERCENT; each account can have its own "Stop at" under More accounts).</p>`;
}

export function availabilityMarkup(data = {}) {
  const holds = data.holds || {};
  return `<div class="settings-card"><h3>Availability</h3><ul class="plain-list">${holdMarkup('Claude', holds.claude)}${holdMarkup('Codex', holds.codex)}</ul><p class="field-hint">When a provider reports a usage limit, a rate limit or a sign-in problem, LAIka pauses it until the time it gave. Builds that only it can do wait without using up an attempt; roles that can, switch to the other provider.</p>${usageMarkup(data.usage?.claude)}${usageMarkup(data.usage?.codex, 'Codex')}</div>`;
}
