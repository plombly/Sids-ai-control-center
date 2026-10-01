import { asObject, duration, esc, number, pill, text } from './format.js';

export const workerMarkup = w => {
  const busy = /^(working|busy|claimed|running|active|stopping)$/i.test(text(w.status, '')) || Boolean(w.active_job_id);
  const removeLabel = busy ? 'Remove (busy)' : 'Remove';
  return `<div class="entity"><div class="entity-head"><span class="entity-name">${esc(w.id)}</span>${pill(w.status)}</div><div class="subtle">${esc(w.provider || 'Provider unknown')} · ${esc(w.model || 'model unknown')} · ${esc(w.role || 'role unknown')}</div><div class="stats"><span>job <b>${esc(w.job_id || 'none')}</b> · effective <b>${number(w.effective_tokens)}</b> · cached <b>${number(w.cached_input_tokens)}</b></span><span>commands <b>${number(w.command_count)}</b> · duration <b>${duration(w.duration)}</b> · heartbeat <b>${esc(w.heartbeat_age == null ? '—' : `${w.heartbeat_age}s ago`)}</b></span></div><div class="actions"><button data-worker="${esc(w.id)}" data-action="start">Start</button><button data-worker="${esc(w.id)}" data-action="stop">Stop</button><button class="danger-button" data-worker="${esc(w.id)}" data-action="remove"${busy ? ' disabled title="Busy worker: stop it before removing it"' : ''}>${removeLabel}</button></div></div>`;
};
export const jobActionsMarkup = job => {
  const actions =
    {
      awaiting_review: [
        ['Reject', 'reject'],
        ['Reintegrate', 'reintegrate']
      ],
      needs_human: [
        ['Reject', 'reject'],
        ['Extend (+1)', 'extend'],
        ['Reintegrate', 'reintegrate']
      ],
      blocked_failed_dependency: [['Reopen', 'reopen']]
    }[text(job.status, '')] || [];
  return actions
    .map(
      ([label, operation]) =>
        `<button data-op="${esc(operation)}" data-job="${esc(job.id)}" data-status="${esc(job.status)}">${esc(label)}</button>`
    )
    .join('');
};
// Preview of a change's running app, on its approval card.
export const previewMarkup = (j, hostname = globalThis.location?.hostname || 'localhost') => {
  if (!j?.previewable) return '';
  const preview = j.preview;
  const id = esc(j.id);
  if (!preview) return `<button type="button" data-preview-start="${id}">Preview</button>`;
  if (['requested', 'starting'].includes(preview.state))
    return `<span class="subtle">Starting preview…</span><button type="button" class="detail-button" data-preview-stop="${id}">Cancel</button>`;
  if (preview.state === 'running' && preview.port) {
    const url = `http://${hostname}:${preview.port}/`;
    return `<a class="button" href="${esc(url)}" target="_blank" rel="noopener">Open preview</a><button type="button" class="detail-button" data-preview-stop="${id}">Stop preview</button>`;
  }
  return `<span class="form-status">Preview ${esc(preview.state)}: ${esc(preview.error || '')}</span><button type="button" data-preview-start="${id}">Retry preview</button>`;
};

export const approvalMarkup = j =>
  `<div class="item approval-item"><div class="item-head"><button class="item-title detail-button" data-detail="${esc(j.id)}">${esc(j.id)}</button>${pill('ready')}</div><p>Review passed · candidate ${esc(j.integrated_candidate_commit || 'state unavailable')}</p><code>python scripts/job-review.py approve ${esc(j.id)}</code><code>python scripts/job-review.py reject ${esc(j.id)}</code>${j.integrated_candidate_commit ? `<button data-op="approve" data-job="${esc(j.id)}" data-status="${esc(j.status)}" data-candidate="${esc(j.integrated_candidate_commit)}">Approve</button>` : ''}${previewMarkup(j)}${jobActionsMarkup(j)}<button data-handoff="${esc(j.goal_id || j.id)}">Copy for ChatGPT</button><button data-download="${esc(j.goal_id || j.id)}">Download handoff</button></div>`;

const detailValue = value => `<span>${esc(value)}</span>`;
const detailRows = (job, fields) =>
  fields
    .map(([label, field]) => `<div class="detail-row"><b>${esc(label)}</b>${detailValue(job[field])}</div>`)
    .join('');
const detailSection = (title, content) => `<section class="detail-block"><h3>${esc(title)}</h3>${content}</section>`;
export const jobDetailMarkup = job => {
  const lineage = asObject(job.lineage) || {},
    reviewHistory = Array.isArray(lineage.review_findings_history) ? lineage.review_findings_history : [],
    sourceCommits = Array.isArray(lineage.source_candidate_commits) ? lineage.source_candidate_commits : [],
    related = Array.isArray(job.related) ? job.related : [],
    historyMarkup = reviewHistory.length
      ? `<ol>${reviewHistory
          .map(
            item =>
              `<li>${detailRows(item, [
                ['Review job', 'review_job_id'],
                ['Candidate', 'candidate'],
                ['Findings', 'findings']
              ])}</li>`
          )
          .join('')}</ol>`
      : detailValue(null),
    sourceMarkup = sourceCommits.length
      ? `<ol>${sourceCommits.map(commit => `<li>${detailValue(commit)}</li>`).join('')}</ol>`
      : detailValue(null),
    gate = asObject(job.gate),
    gateMarkup = gate
      ? detailRows(gate, [
          ['Return code', 'returncode'],
          ['Summary', 'summary']
        ])
      : '<p>No gate result</p>',
    relatedMarkup = related.length
      ? `<div class="table-wrap"><table class="job-table"><thead><tr><th>Job</th><th>Role</th><th>Status</th><th>Review verdict</th><th>Duration</th><th>Tokens</th><th>Created</th></tr></thead><tbody>${related.map(item => `<tr><td><button class="detail-button" data-detail="${esc(item.id)}">${esc(item.id)}</button></td><td>${esc(item.role)}</td><td>${esc(item.status)}</td><td>${esc(item.review_verdict)}</td><td>${esc(item.duration)}</td><td>${esc(item.effective_tokens)}</td><td>${esc(item.created_at)}</td></tr>`).join('')}</tbody></table></div>`
      : detailValue(null);
  return `<div class="detail-title"><h2>${detailValue(job.id)}</h2>${pill(job.status)}</div><section class="job-log-section"><h3>Live log</h3><div class="job-log" data-job-log="${esc(job.id)}"><div class="subtle">Loading…</div></div><button type="button" class="detail-button" data-job-log-tests="${esc(job.id)}">Show test output</button><pre class="job-test-log" hidden></pre></section>${detailSection(
    'Summary',
    detailRows(job, [
      ['ID', 'id'],
      ['Title', 'title'],
      ['Status', 'status'],
      ['Role', 'role'],
      ['Worker', 'worker'],
      ['Provider', 'provider'],
      ['Model', 'model'],
      ['Review status', 'review_status'],
      ['Review verdict', 'review_verdict'],
      ['Integration status', 'integration_status'],
      ['Duration', 'duration'],
      ['Effective tokens', 'effective_tokens'],
      ['Cached input tokens', 'cached_input_tokens'],
      ['Output tokens', 'output_tokens'],
      ['Command count', 'command_count'],
      ['Files', 'files'],
      ['Tests', 'tests'],
      ['Error', 'error']
    ])
  )}${detailSection(
    'Candidate',
    detailRows(job, [
      ['Goal ID', 'goal_id'],
      ['Branch', 'branch'],
      ['Base', 'base'],
      ['Candidate', 'candidate'],
      ['Integration base commit', 'integration_base_commit'],
      ['Integrated candidate commit', 'integrated_candidate_commit'],
      ['Reviewed commit', 'reviewed_commit'],
      ['Integration worktree', 'integration_worktree'],
      ['Integration branch', 'integration_branch']
    ])
  )}${detailSection(
    'Attempts',
    detailRows({ ...job, ...lineage }, [
      ['Build attempt', 'build_attempt'],
      ['Max build attempts', 'max_build_attempts'],
      ['Review recoveries', 'review_recoveries'],
      ['Retry reason', 'retry_reason'],
      ['Repair status', 'repair_status'],
      ['Repair attempts', 'repair_attempts'],
      ['Max repair attempts', 'max_repair_attempts'],
      ['Needs human kind', 'needs_human_kind'],
      ['Repair job ID', 'repair_job_id'],
      ['Last repair job ID', 'last_repair_job_id'],
      ['Last integrate job ID', 'last_integrate_job_id'],
      ['Needs human reason', 'needs_human_reason']
    ])
  )}${detailSection(
    'Review',
    detailRows(job, [
      ['Review job ID', 'review_job_id'],
      ['Review findings', 'review_findings']
    ]) + `<h4>Source candidate commits</h4>${sourceMarkup}<h4>Review history</h4>${historyMarkup}`
  )}${detailSection('Gate', gateMarkup)}${detailSection('Related jobs', relatedMarkup)}`;
};
export const tokenStateText = auth =>
  !auth?.token_required
    ? 'Writes open (no token set on server)'
    : auth.token_valid
      ? 'Actions enabled'
      : 'Token needed for actions';
