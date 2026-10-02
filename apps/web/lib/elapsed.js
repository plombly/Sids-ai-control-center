export function elapsedText(seconds) {
  if (seconds == null) return '';
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return '';
  const total = Math.floor(value);
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
  if (total < 86400) return `${Math.floor(total / 3600)}h ${String(Math.floor(total / 60) % 60).padStart(2, '0')}m`;
  return `${Math.floor(total / 86400)}d ${Math.floor(total / 3600) % 24}h`;
}

export function clockText(epochSeconds, now = Date.now() / 1000) {
  if (epochSeconds == null) return '';
  const date = new Date(Number(epochSeconds) * 1000);
  const current = new Date(Number(now) * 1000);
  if (Number.isNaN(date.getTime()) || Number.isNaN(current.getTime())) return '';
  const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  if (date.getFullYear() === current.getFullYear() && date.getMonth() === current.getMonth() && date.getDate() === current.getDate()) return time;
  return `${date.toLocaleString('en-US', { month: 'short' })} ${date.getDate()} ${time}`;
}

export function elapsedMarkup(startedAt, finishedAt, now = Date.now() / 1000) {
  if (startedAt == null) return '';
  if (finishedAt == null) return `<span class="elapsed" data-elapsed-since="${startedAt}">${elapsedText(now - startedAt)}</span>`;
  return `took ${elapsedText(finishedAt - startedAt)} · finished ${clockText(finishedAt, now)}`;
}

if (typeof window !== 'undefined' && typeof document !== 'undefined' && !globalThis.__sidElapsedTimer) {
  globalThis.__sidElapsedTimer = true;
  setInterval(() => {
    document.querySelectorAll('[data-elapsed-since]').forEach(element => {
      element.textContent = elapsedText(Date.now() / 1000 - Number(element.getAttribute('data-elapsed-since')));
    });
  }, 1000);
}
