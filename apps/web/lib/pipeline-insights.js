// Pipeline insights feature module: renders non-actionable job insights in its own panel.
import { asObject, esc, pill, text } from './format.js';
import { registerPanel } from './registry.js';

const nonEmpty = value => typeof value === 'string' && value.trim() !== '';
const jobButton = id => `<button type="button" class="item-title" data-detail="${esc(id)}">${esc(id)}</button>`;
const item = (id, details) =>
  `<div class="item"><div class="item-head">${jobButton(id)}</div><div class="subtle">${details}</div></div>`;
const section = (title, items) =>
  `<h3>${title}</h3>${items.length ? `<div class="stack">${items.join('')}</div>` : '<div class="empty">None</div>'}`;
const testsValue = value =>
  value === true || value === 'pass' || value === 'passed'
    ? 'yes'
    : value === false || value === 'fail' || value === 'failed'
      ? 'no'
      : '—';

const sideDetails = (label, side) => {
  if (!asObject(side)) return `${label}: —`;
  const details = `${label}: ${esc(text(side.provider))}, tests: ${testsValue(side.tests)}, lines: ${esc(
    text(side.lines)
  )}`;
  return nonEmpty(side.error) ? `${details}, error: ${esc(side.error.trim())}` : details;
};

export function insightsMarkup(jobs) {
  const entries = Array.isArray(jobs)
    ? jobs.filter(job => job && typeof job === 'object' && !Array.isArray(job))
    : [];
  const waiting = [];
  const provider = [];
  const bestOf = [];
  const reviews = [];

  for (const job of entries) {
    const id = text(job.id);
    if (nonEmpty(job.blocked_reason)) waiting.push(item(id, esc(job.blocked_reason.trim())));

    const providerWait = nonEmpty(job.provider_wait);
    const providerFallback = nonEmpty(job.provider_fallback);
    if (providerWait || providerFallback) {
      const messages = [`${esc(text(job.provider))}/${esc(text(job.model))}`];
      if (providerWait) messages.push(`Wait: ${esc(job.provider_wait.trim())}`);
      if (providerFallback) messages.push(`Fallback: ${esc(job.provider_fallback.trim())}`);
      provider.push(item(id, messages.join('<br>')));
    }

    const best = asObject(job.best_of);
    if (best) {
      if (nonEmpty(best.skipped)) bestOf.push(item(id, `Skipped: ${esc(best.skipped.trim())}`));
      else {
        bestOf.push(
          item(
            id,
            [`Chosen: ${esc(text(best.chosen))}`, sideDetails('primary', best.primary), sideDetails('alt', best.alt)].join(
              '<br>'
            )
          )
        );
      }
    }

    const aspects = asObject(job.review_aspects);
    if (aspects && Object.keys(aspects).length) {
      reviews.push(
        item(
          id,
          Object.entries(aspects)
            .map(([aspect, verdict]) => pill(`${aspect}: ${text(verdict)}`))
            .join(' ')
        )
      );
    }
  }

  return [
    section('Waiting for files', waiting),
    section('Provider waits and fallbacks', provider),
    section('Best-of builds', bestOf),
    section('Specialist reviews', reviews)
  ].join('');
}

function renderInsights(state) {
  if (typeof document === 'undefined') return;
  let panel = document.getElementById('pipeline-insights-panel');
  if (!panel) {
    const grid = document.querySelector('.dashboard-grid');
    if (!grid) return;
    panel = document.createElement('section');
    panel.className = 'panel wide';
    panel.id = 'pipeline-insights-panel';
    panel.innerHTML =
      '<div class="panel-heading"><div><p class="eyebrow">PIPELINE</p><h2>Parallel activity</h2></div></div>' +
      '<div id="pipeline-insights-content"></div>';
    grid.append(panel);
  }
  const content = document.getElementById('pipeline-insights-content');
  if (content) content.innerHTML = insightsMarkup(state?.jobs?.data);
}

registerPanel(renderInsights);
