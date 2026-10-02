// The name and logo (brand/brand.json). A server can be rebranded only on
// the host (/etc/laika/branding.json + `sudo laika branding apply`, which
// nginx serves in place of the shipped files); never from the dashboard.
import { esc, escValue } from './format.js';

export const DEFAULT_BRAND = {
  name: 'LAIka',
  wordmark: ['L', 'AI', 'ka'],
  tagline: 'AI software builder',
  logo: 'brand/logo.svg',
  favicon: 'brand/favicon.svg'
};
let brand = DEFAULT_BRAND;
export const currentBrand = () => brand;

const clean = (value, fallback, limit = 60) => (typeof value === 'string' && value.trim() ? value.trim().slice(0, limit) : fallback);
const LOCAL = /^brand\/[A-Za-z0-9._-]+\.(svg|png)$/;

export function normalizeBrand(data = {}) {
  const parts = Array.isArray(data.wordmark) && data.wordmark.length && data.wordmark.every(part => typeof part === 'string') ? data.wordmark.slice(0, 5).map(part => part.slice(0, 30)) : null;
  const name = clean(data.name, DEFAULT_BRAND.name, 40);
  return {
    name,
    wordmark: parts || (data.name ? [name] : DEFAULT_BRAND.wordmark),
    tagline: clean(data.tagline, DEFAULT_BRAND.tagline, 80),
    logo: LOCAL.test(data.logo || '') ? data.logo : DEFAULT_BRAND.logo,
    favicon: LOCAL.test(data.favicon || '') ? data.favicon : DEFAULT_BRAND.favicon
  };
}

// Parts alternate plain / accented: ["L", "AI", "ka"] colours only "AI".
export function wordmarkHtml(b = brand) {
  return `<span class="wordmark">${b.wordmark.map((part, index) => (index % 2 ? `<span class="wordmark-ai">${esc(part)}</span>` : esc(part))).join('')}</span>`;
}

export const logoHtml = (size, className, b = brand) => `<img class="${escValue(className)}" src="${escValue(b.logo)}" alt="" width="${size}" height="${size}">`;

export function applyBrand(b, doc = globalThis.document) {
  brand = b;
  if (!doc) return;
  doc.title = b.name;
  const mark = doc.querySelector('.brand .wordmark');
  if (mark) mark.outerHTML = wordmarkHtml(b).replace('<span class="wordmark">', '<strong class="wordmark">').replace(/<\/span>$/, '</strong>');
  const tagline = doc.querySelector('.brand small');
  if (tagline) tagline.textContent = b.tagline;
  doc.querySelectorAll('img.brand-logo, img.auth-logo').forEach(img => img.setAttribute('src', b.logo));
  const icon = doc.querySelector('link[rel="icon"]');
  if (icon) icon.setAttribute('href', b.favicon);
}

if (typeof document !== 'undefined' && typeof fetch === 'function') {
  fetch('brand/brand.json', { cache: 'no-cache' })
    .then(response => (response.ok ? response.json() : {}))
    .then(data => applyBrand(normalizeBrand(data)))
    .catch(() => {});
}
