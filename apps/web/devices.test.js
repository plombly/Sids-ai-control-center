import test from 'node:test';
import assert from 'node:assert/strict';
import { devicesMarkup, pairingMarkup, suggestedUrl } from './lib/devices.js';

test('phones list, add form and revoke', () => {
  const now = 100000;
  const html = devicesMarkup({ server_name: 'Home <LAIka>', devices: [{ id: 'abc123abc123', name: '<Phone>', created_at: now - 3600 * 3, last_used_at: now - 60, last_seen_from: '10.8.0.6' }] }, 'http://192.168.1.20:8000', now);
  assert.match(html, /&lt;Phone&gt;/);
  assert.match(html, /last used just now from 10\.8\.0\.6/);
  assert.match(html, /data-device-revoke="abc123abc123"/);
  assert.match(html, /value="http:\/\/192\.168\.1\.20:8000"/);
  assert.match(html, /value="Home &lt;LAIka&gt;"/);
  assert.match(devicesMarkup({}), /No phones yet/);
  assert.equal(suggestedUrl({ hostname: '192.168.1.20', port: '8080' }), 'http://192.168.1.20:8080');
});

test('the pairing code is shown once with its QR picture', () => {
  const html = pairingMarkup({ device: { name: 'Phone' }, qr_svg: '<svg></svg>', pairing_text: '{"key":"laika_<x>"}' });
  assert.match(html, /<svg><\/svg>/);
  assert.match(html, /laika_&lt;x&gt;/);
  assert.match(html, /data-device-done/);
});
