import test from 'node:test';
import assert from 'node:assert/strict';
import { remoteMarkup } from './lib/remote-access.js';

test('remote access: install, connect, sign-in link, addresses and warnings', () => {
  assert.match(remoteMarkup({ status: { installed: false } }), /data-remote-action="install"/);
  assert.match(remoteMarkup({ status: { installed: true, state: 'needs_login' } }), /Connect to my Tailscale account/);
  const waiting = remoteMarkup({ status: { installed: true, state: 'needs_login' }, login: { state: 'waiting', url: 'https://login.tailscale.com/a/abc' } });
  assert.match(waiting, /href="https:\/\/login\.tailscale\.com\/a\/abc"/);
  const connected = remoteMarkup({ status: { installed: true, state: 'connected', ips: ['100.64.0.5', 'fd7a::5'], dns_name: 'laika.tail1.ts.net', magic_dns: true, tailnet: 'me@x' } }, [['Tank Tumble', 8100]]);
  assert.match(connected, /http:\/\/laika\.tail1\.ts\.net:8080/);
  assert.match(connected, /http:\/\/100\.64\.0\.5:8080/);
  assert.match(connected, /Tank Tumble<\/span><code>http:\/\/laika\.tail1\.ts\.net:8100/);
  assert.match(connected, /data-remote-action="logout"/);
  assert.match(remoteMarkup({ status: { installed: true, state: 'connected', funnel: true, ips: ['100.64.0.5'] } }), /Funnel is ON/);
});
