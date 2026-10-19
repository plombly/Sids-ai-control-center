// Home: "While you were away", what LAIka handled by itself since your last
// look (apps/api/away_routes.py). The last look is remembered per browser.
import { requestJSON } from './api.js';
import { esc } from './format.js';
import { registerClick, registerPanel } from './registry.js';
import { isAdmin } from './access.js';

const SEEN_KEY = 'laika-away-seen';

export function awayMarkup(items = [], now = Date.now() / 1000) {
  if (!items.length) return '';
  const ago = at => {
    const minutes = Math.max(1, Math.round((now - at) / 60));
    return minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.round(minutes / 60)} h ago` : `${Math.round(minutes / 1440)} d ago`;
  };
  const rows = items.slice(0, 8)
    .map(item => `<li><b>${esc(item.title)}</b>: ${esc(item.what)} <span class="subtle">${esc(ago(item.at))}</span> <button type="button" class="detail-button" data-detail="${esc(item.job)}">Details</button></li>`)
    .join('');
  return `<h2>While you were away</h2><ul class="plain-list away-list">${rows}</ul>${items.length > 8 ? `<p class="subtle">and ${esc(items.length - 8)} more</p>` : ''}<button type="button" data-away-seen>Got it</button>`;
}

const seen = () => {
  try {
    return Number(localStorage.getItem(SEEN_KEY)) || Date.now() / 1000 - 86400;
  } catch {
    return Date.now() / 1000 - 86400;
  }
};

let fetchedAt = 0;

async function refresh() {
  const node = document.getElementById('home-away');
  if (!node || !isAdmin() || Date.now() - fetchedAt < 60000) return;
  fetchedAt = Date.now();
  try {
    const data = await requestJSON(`/api/away?since=${encodeURIComponent(seen())}`);
    const html = awayMarkup(data.items || []);
    node.innerHTML = html;
    node.hidden = !html;
  } catch {}
}

if (typeof document !== 'undefined') {
  registerPanel(() => refresh());
  registerClick('awaySeen', () => {
    try {
      localStorage.setItem(SEEN_KEY, String(Date.now() / 1000));
    } catch {}
    const node = document.getElementById('home-away');
    if (node) node.hidden = true;
  });
}
