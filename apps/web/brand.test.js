import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_BRAND, normalizeBrand, wordmarkHtml, logoHtml } from './lib/brand.js';
import { setupMarkup } from './lib/auth.js';

test('the default brand colours only AI', () => {
  assert.equal(wordmarkHtml(DEFAULT_BRAND), '<span class="wordmark">L<span class="wordmark-ai">AI</span>ka</span>');
  assert.match(setupMarkup(), /Welcome to <span class="wordmark">L<span class="wordmark-ai">AI<\/span>ka<\/span>/);
});

test('a custom brand is cleaned before use', () => {
  const brand = normalizeBrand({ name: 'Acme <b>', wordmark: ['Ac', 'me'], logo: 'https://evil.example/x.svg', favicon: 'brand/acme.png' });
  assert.equal(brand.name, 'Acme <b>');
  assert.equal(wordmarkHtml(brand), '<span class="wordmark">Ac<span class="wordmark-ai">me</span></span>');
  assert.equal(brand.logo, DEFAULT_BRAND.logo);
  assert.equal(brand.favicon, 'brand/acme.png');
  assert.deepEqual(normalizeBrand({ name: 'Solo' }).wordmark, ['Solo']);
  assert.deepEqual(normalizeBrand({}), DEFAULT_BRAND);
  assert.match(logoHtml(36, 'brand-logo', brand), /src="brand\/logo\.svg"/);
});
