// Applies Settings → General and Appearance for everyone on this server:
// theme (system / dark / light), accent colour, layout density, text size,
// reduced motion and the home page sections. The last values are kept in
// this browser so the page paints correctly before the server answers.
import { requestJSON } from './api.js';

const CACHE = 'laika-prefs';
export const DEFAULT_PREFS = {
  THEME: 'system', ACCENT: '#5b9dff', DENSITY: 'comfortable', FONT_SCALE: '100', REDUCE_MOTION: 'false',
  HOME_SECTIONS: 'needs,progress,projects,recent', CLOCK: '24h', DATE_FORMAT: 'short', DASHBOARD_REFRESH_SECONDS: '5', DEFAULT_IMPORTANCE: 'medium', SERVER_NAME: 'LAIka'
};

export function prefs() {
  return { ...DEFAULT_PREFS, ...(globalThis.LAIKA_PREFS || {}) };
}

export function applyPrefs(values, doc = globalThis.document) {
  const merged = { ...DEFAULT_PREFS, ...values };
  globalThis.LAIKA_PREFS = merged;
  if (!doc?.documentElement) return merged;
  const html = doc.documentElement;
  html.dataset.theme = merged.THEME;
  html.dataset.density = merged.DENSITY;
  if (/^#[0-9a-f]{6}$/i.test(merged.ACCENT)) html.style.setProperty('--accent', merged.ACCENT);
  const scale = Math.min(130, Math.max(85, Number(merged.FONT_SCALE) || 100));
  html.style.fontSize = `${scale}%`;
  html.toggleAttribute('data-reduce-motion', merged.REDUCE_MOTION === 'true');
  const order = String(merged.HOME_SECTIONS).split(',').filter(Boolean);
  doc.querySelectorAll('[data-home-section]').forEach(section => {
    const index = order.indexOf(section.dataset.homeSection);
    section.hidden = index < 0;
    section.style.order = String(index < 0 ? 99 : index + 2);
  });
  doc.getElementById('home')?.classList.toggle('home-custom-order', order.join(',') !== DEFAULT_PREFS.HOME_SECTIONS);
  return merged;
}

function cached() {
  try {
    return JSON.parse(globalThis.localStorage?.getItem(CACHE) || '{}');
  } catch {
    return {};
  }
}

function remember(values) {
  try {
    globalThis.localStorage?.setItem(CACHE, JSON.stringify(values));
  } catch {}
}

const pick = values => Object.fromEntries(Object.keys(DEFAULT_PREFS).filter(key => key in (values || {})).map(key => [key, values[key]]));

if (typeof document !== 'undefined') {
  applyPrefs(cached());
  requestJSON('/api/settings')
    .then(data => {
      const values = pick(data.values);
      applyPrefs(values);
      remember(values);
    })
    .catch(() => {});
  window.addEventListener('laika:settings-changed', event => {
    const values = pick(event.detail);
    applyPrefs(values);
    remember(values);
  });
}
