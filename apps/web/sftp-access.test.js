import test from 'node:test';
import assert from 'node:assert/strict';
import { sftpMarkup } from './lib/sftp-access.js';
import { MEMBER_SECTIONS } from './lib/system-settings.js';

test('SFTP access: address, server key, keys list, notices', () => {
  const html = sftpMarkup({ online: true, enabled: true, port: 2222, fingerprint: 'SHA256:abc', username: 'Sam', commit_seconds: 30 },
    [{ id: 'k1', name: 'Laptop <1>', type: 'ssh-ed25519', fingerprint: 'SHA256:xyz', added: 1 }], 'laika.lan');
  assert.match(html, /sftp:\/\/Sam@laika\.lan:2222/);
  assert.match(html, /SHA256:abc/);
  assert.match(html, /Laptop &lt;1&gt;/);
  assert.match(html, /data-ssh-remove="k1"/);
  assert.match(html, /30 seconds after your last change/);
  assert.match(sftpMarkup({}, [], 'h'), /not running/);
  assert.match(sftpMarkup({ online: true, enabled: false }, [], 'h'), /switched off/);
  assert.ok(MEMBER_SECTIONS.includes('my-sftp'));
});
