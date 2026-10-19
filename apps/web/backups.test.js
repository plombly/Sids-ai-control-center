import { test } from 'node:test';
import assert from 'node:assert/strict';
import { backupsMarkup } from './lib/backups.js';

test('snapshots with size, contents and problems; the restore check; run buttons', () => {
  const html = backupsMarkup({
    snapshots: [{ name: '20261019T083000Z', bytes: 2.5e9, parts: ['redis', 'repo'], errors: { git_remote: 'refused' } },
                { name: '20261018T083000Z', bytes: 4e6, parts: ['repo'], errors: {} }],
    restore_check: { ok: true, snapshot: '20261019T083000Z', checks: [] }
  });
  assert.match(html, /2\.5 GB<\/td><td>redis, repo<\/td><td><span class="pill warn">git_remote failed/);
  assert.match(html, /<span class="pill ok">complete/);
  assert.match(html, /Last restore check: restored fine/);
  assert.match(html, /data-backup-run="backup">Back up now/);
  assert.match(backupsMarkup({}), /No snapshots yet/);
});
