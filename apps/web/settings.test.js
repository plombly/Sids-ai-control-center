import test from 'node:test';
import assert from 'node:assert/strict';
import { targetsMarkup, rulesMarkup, rulesFromForm } from './lib/settings.js';

test('targets: write-only fields, set state with hint, remove buttons', () => {
  const html = targetsMarkup({ discord: true, discord_hint: '…xyz9', mention: '123', ntfy: false });
  assert.match(html, /Set …xyz9/);
  assert.match(html, /name="discord_webhook" type="password"/);
  assert.match(html, /data-target-clear="discord_webhook"/);
  assert.doesNotMatch(html, /data-target-clear="ntfy_url"/);
  assert.match(html, /data-notify-test/);
});

test('rules: one select per event, quiet hours, digest schedule', () => {
  const html = rulesMarkup({
    settings: { events: { approval: 'ping' }, quiet: { enabled: true, start: '23:00', end: '06:00' }, digest: { day: 'fri', time: '09:30' } },
    events: [{ type: 'approval', label: 'A change is ready', urgent: false }, { type: 'health_red', label: 'Health red', urgent: true }],
    days: ['mon', 'fri', 'sun']
  });
  assert.match(html, /name="event:approval"[^>]*><option value="ping" selected>/);
  assert.match(html, /Health red <span class="subtle">\(even in quiet hours\)/);
  assert.match(html, /name="quiet_enabled" checked/);
  assert.match(html, /<option value="fri" selected>Friday/);
  const form = { entries: [['event:approval', 'off'], ['quiet_enabled', 'on'], ['quiet_start', '22:30'], ['digest_day', 'mon'], ['digest_time', '08:00']] };
  globalThis.FormData = class { constructor(f) { this.map = new Map(f.entries); } get(k) { return this.map.get(k) ?? null; } };
  assert.deepEqual(rulesFromForm(form, ['approval', 'goal_done']), {
    events: { approval: 'off', goal_done: 'post' },
    quiet: { enabled: true, start: '22:30', end: '07:00' },
    digest: { day: 'mon', time: '08:00' }
  });
});
