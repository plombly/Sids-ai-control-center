import test from 'node:test';
import assert from 'node:assert/strict';
import { usageMarkup, money, compact } from './lib/usage.js';

test('usage panel: headline per provider, projects by cost, day bars, ranges', () => {
  assert.equal(money(7.7206), '$7.72');
  assert.equal(compact(5015410), '5.0M');
  assert.equal(compact(1500), '2k');
  const html = usageMarkup({
    total: { jobs: 3, seconds: 7200, effective_tokens: 3000 },
    by_provider: [{ provider: 'claude', jobs: 1, cost_usd: 0.25, effective_tokens: 500 }, { provider: 'codex', jobs: 2, cost_usd: 0, effective_tokens: 2500 }],
    by_project: [{ project: 'sid', jobs: 1, cost_usd: 0, effective_tokens: 10 }, { project: '<shop>', jobs: 2, cost_usd: 0.25, effective_tokens: 2990 }],
    by_day: [{ day: '2026-09-30', jobs: 3, cost_usd: 0.25, effective_tokens: 3000 }],
    by_role: [{ role: 'builder', jobs: 2, cost_usd: 0 }]
  }, 30);
  assert.match(html, /\$0\.25<\/span><span class="subtle">Claude, 1 runs/);
  assert.match(html, /2\.0 h of agent time/);
  assert.ok(html.indexOf('&lt;shop&gt;') < html.indexOf('>sid<'));
  assert.match(html, /class="active" data-usage-days="30"/);
  assert.match(html, /usage-bar/);
  assert.equal(usageMarkup(null), '<div class="empty">No usage yet</div>');
});
