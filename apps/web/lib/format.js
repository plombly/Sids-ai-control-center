export const TERMINAL =
  /^(completed|completed_no_changes|merged|done|succeeded|failed|error|integration_failed|queue_failed|test_failed|rejected|repair_exhausted|blocked_failed_dependency|planning_failed)$/i;
export const asObject = value => (value && typeof value === 'object' && !Array.isArray(value) ? value : null);
export const asArray = value => (Array.isArray(value) ? value.filter(item => asObject(item)) : []);
const finite = value => (typeof value === 'number' && Number.isFinite(value) ? value : null);
export const text = (value, fallback = '—') =>
  value === null || value === undefined || value === '' ? fallback : String(value);
export const esc = value =>
  text(value).replace(
    /[&<>"']/g,
    char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]
  );
// For input values and other places where empty must stay empty (esc shows "—").
export const escValue = value =>
  (value === null || value === undefined ? '' : String(value)).replace(
    /[&<>"']/g,
    char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]
  );
export const number = value => (finite(value) === null ? '—' : Number(value).toLocaleString());
export const duration = value =>
  finite(value) === null
    ? '—'
    : value < 60
      ? `${Math.round(value)}s`
      : `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
const statusClass = value =>
  /fail|error|offline/i.test(text(value, ''))
    ? 'bad'
    : /wait|review|pending|idle|blocked|human|disabled/i.test(text(value, ''))
      ? 'warn'
      : /complete|success|active|running|online|healthy|ready/i.test(text(value, ''))
        ? 'ok'
        : '';
export const pill = value => `<span class="pill ${statusClass(value)}">${esc(value)}</span>`;
