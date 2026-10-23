// The rendered preview of a candidate on LNMT (services/visual_preview.py):
// screenshots of its pages at desktop and phone width, and what the checks
// noticed, on the approval card. Thumbnails open the full screenshot.
import { esc, escValue } from './format.js';

const shotUrl = (job, file) => `/api/jobs/${encodeURIComponent(job.id)}/visual/${encodeURIComponent(file)}`;

export function visualMarkup(job) {
  const state = job?.visual_status;
  if (!state) return '';
  if (state === 'pending' || state === 'running') return '<p class="visual-note subtle">Rendering a preview of this change on LNMT…</p>';
  if (state === 'failed') return `<p class="visual-note warn-text">No preview: ${esc(job.visual_error || 'LNMT could not render it')}.</p>`;
  const pages = job.visual?.pages || [];
  if (!pages.length) return '';
  const problems = pages.flatMap(page => Object.entries(page.problems || {}).flatMap(([width, list]) =>
    (list || []).map(problem => `${page.path === '/' ? 'Home' : page.path} (${width}): ${problem}`)));
  const shots = pages.map(page => ['desktop', 'phone'].filter(width => page.shots?.[width]).map(width =>
    `<a class="visual-shot ${width}" href="${escValue(shotUrl(job, page.shots[width]))}" target="_blank" rel="noopener" title="${escValue(`${page.title || page.path} · ${width}`)}"><img src="${escValue(shotUrl(job, page.shots[width]))}" alt="${escValue(`${page.path} at ${width} width`)}" loading="lazy"><span>${esc(page.path === '/' ? 'Home' : page.path)} · ${esc(width)}</span></a>`).join('')).join('');
  return `<details class="visual" open><summary>How it looks${problems.length ? ` · <span class="warn-text">${esc(problems.length)} thing${problems.length === 1 ? '' : 's'} to check</span>` : ''}</summary>
    ${problems.length ? `<ul class="plain-list visual-problems">${problems.slice(0, 8).map(p => `<li>${esc(p)}</li>`).join('')}</ul>` : ''}
    <div class="visual-shots">${shots}</div>${job.visual?.url ? `<p class="subtle">Try it: <a href="${escValue(job.visual.url)}" target="_blank" rel="noopener">${esc(job.visual.url)}</a> (a throwaway copy on LNMT for 6 hours, from the LAN or VPN)</p>` : ''}</details>`;
}
