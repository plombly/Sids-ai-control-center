import { test } from 'node:test';
import assert from 'node:assert/strict';
import { visualMarkup } from './lib/visual-preview.js';

test('rendered previews on the approval card', () => {
  assert.equal(visualMarkup({}), '');
  assert.match(visualMarkup({ visual_status: 'running' }), /Rendering a preview/);
  assert.match(visualMarkup({ visual_status: 'failed', visual_error: 'build failed' }), /No preview: build failed/);
  const html = visualMarkup({ id: 'j1', visual_status: 'done', visual: { url: 'http://10.0.0.121:30000/', pages: [
    { path: '/', title: 'Home', shots: { desktop: 'home-desktop.png', phone: 'home-phone.png' },
      problems: { desktop: ['words broken across lines: Commuter'], phone: [] } }] } });
  assert.match(html, /src="\/api\/jobs\/j1\/visual\/home-desktop\.png"/);
  assert.match(html, /1 thing to check/);
  assert.match(html, /Home \(desktop\): words broken across lines: Commuter/);
  assert.match(html, /href="http:\/\/10\.0\.0\.121:30000\/"/);
});
