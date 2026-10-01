import test from 'node:test';
import assert from 'node:assert/strict';
import {
  projectListMarkup,
  projectDetailMarkup,
  publicKeyMarkup,
  buildProjectRequest,
  validateProjectValues,
  deleteProjectMarkup,
  buildSettingsMarkup,
  appStatusMarkup,
  buildSettingsRequest,
  systemInfoMarkup,
  trashMarkup
} from './lib/projects.js';

test('project list markup renders projects and empty state', () => {
  const markup = projectListMarkup([{
    id: 'web-shop',
    name: '<b>x</b>',
    importance: 'high',
    status: 'paused',
    counts: { jobs_queued: 1, jobs_running: 2, jobs_awaiting_approval: 3, jobs_needs_human: 4, jobs_merged: 5 },
    stats: { remaining_effort: 6 }
  }]);
  assert.match(markup, /href="#\/projects\/web-shop"/);
  assert.match(markup, /queued 1 · running 2 · awaiting approval 3 · needs human 4 · merged 5/);
  assert.match(markup, /remaining effort 6/);
  assert.match(markup, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.doesNotMatch(markup, /<b>x<\/b>/);
  assert.equal(projectListMarkup([]), '<div class="empty">No projects yet</div>');
});

test('project detail markup renders forms, retry state, and jobs', () => {
  const markup = projectDetailMarkup({
    id: 'web-shop', name: '<Project>', importance: 'high', status: 'pending_key',
    goals: [{ prompt: '<goal>', status: 'running', progress: { completed: 1, total: 2 } }],
    jobs: [{ id: 'job-1', status: 'queued', review_verdict: '<none>', provider: 'openai', model: 'gpt' }]
  });
  assert.match(markup, /<option value="high" selected>/);
  assert.match(markup, /data-retry-clone="web-shop"/);
  assert.match(markup, /id="project-goal-form"/);
  assert.match(markup, /id="project-push-form"/);
  assert.match(markup, /data-detail="job-1"/);
  assert.match(markup, /&lt;Project&gt;/);
  assert.match(markup, /&lt;goal&gt;/);
  assert.doesNotMatch(projectDetailMarkup({ id: 'x', status: 'active' }), /data-retry-clone/);
});

test('public key markup handles valid and invalid output', () => {
  assert.match(publicKeyMarkup(JSON.stringify({ public_key: '<key>' })), /&lt;key&gt;/);
  assert.equal(publicKeyMarkup(JSON.stringify({ message: 'no key' })), '');
  assert.equal(publicKeyMarkup('{bad json'), '');
});

test('project request trims values and conditionally includes fields', () => {
  const empty = buildProjectRequest({ id: ' x ', name: ' Name ', importance: ' medium ', source: '', url: 'ignored', push_remote: ' ', gate: '' });
  assert.deepEqual({ ...empty, request_id: undefined }, { id: 'x', name: 'Name', importance: 'medium', source: '', request_id: undefined });
  assert.equal(typeof empty.request_id, 'string');
  const clone = buildProjectRequest({ id: 'x', name: 'Name', importance: 'low', source: 'clone', url: ' https://github.com/x ', push_remote: ' origin ', gate: ' ci ' });
  assert.equal(clone.url, 'https://github.com/x');
  assert.equal(clone.push_remote, 'origin');
  assert.equal(clone.gate, 'ci');
});

test('project values validate id, name, and clone URL', () => {
  assert.match(validateProjectValues({ id: 'Bad_Id', name: 'Name' }), /ID/);
  assert.equal(validateProjectValues({ id: 'good-id', name: 'Name' }), '');
  assert.match(validateProjectValues({ id: 'good-id', name: '' }), /Name/);
  assert.match(validateProjectValues({ id: 'good-id', name: 'x'.repeat(81) }), /Name/);
  assert.match(validateProjectValues({ id: 'good-id', name: 'Name', source: 'clone' }), /URL/);
});


test('list shows the counts the API actually returns (jobs_* keys)', async () => {
  const { projectListMarkup } = await import('./lib/projects.js');
  const html = projectListMarkup([{ id: 'sid', name: 'SID', importance: 'medium', status: 'active',
    counts: { jobs_queued: 2, jobs_running: 1, jobs_awaiting_approval: 3, jobs_needs_human: 0, jobs_merged: 42 },
    stats: { remaining_effort: 5 } }]);
  assert.match(html, /queued 2 · running 1 · awaiting approval 3 · needs human 0 · merged 42/);
});

test('delete section on every project page except SID itself', () => {
  assert.match(deleteProjectMarkup('web-shop'), /id="project-delete-form"/);
  assert.match(deleteProjectMarkup('web-shop'), /placeholder="Type web-shop to confirm"/);
  assert.match(deleteProjectMarkup('web-shop'), /class="danger-button"/);
  assert.equal(deleteProjectMarkup('sid'), '');
  assert.match(projectDetailMarkup({ id: 'web-shop', status: 'active' }), /project-delete-form/);
  assert.doesNotMatch(projectDetailMarkup({ id: 'sid', status: 'active' }), /project-delete-form/);
});

test('build & run settings: empty fields stay empty, SID has none', () => {
  const html = buildSettingsMarkup({ id: 'shop', setup_command: '', gate_command: 'npm test', run_command: '' });
  assert.match(html, /id="project-settings-form"/);
  assert.match(html, /name="setup_command" value=""/);
  assert.match(html, /name="gate_command" value="npm test"/);
  assert.doesNotMatch(html, /value="—"/);
  assert.doesNotMatch(html, /app-status/);
  assert.equal(buildSettingsMarkup({ id: 'sid' }), '');
  assert.deepEqual(buildSettingsRequest({ setup_command: ' npm ci ', gate_command: '', run_command: 'npm start', run_port: '' }),
    { setup_command: 'npm ci', gate_command: '', run_command: 'npm start' });
  assert.equal(buildSettingsRequest({ run_port: '8105' }).run_port, 8105);
});

test('app status links to the app on this server only when running', () => {
  const running = appStatusMarkup({ id: 'shop', run_command: 'npm start', app: { state: 'running', port: 8100, commit: 'abcdef123456' } }, '10.0.0.59');
  assert.match(running, /href="http:\/\/10.0.0.59:8100\/"/);
  assert.match(running, /data-app-restart="shop"/);
  assert.match(running, /main abcdef12/);
  const crashed = appStatusMarkup({ id: 'shop', run_command: 'npm start', app: { state: 'crashed', port: 8100, log: '<err>' } }, 'h');
  assert.doesNotMatch(crashed, /href=/);
  assert.match(crashed, /&lt;err&gt;/);
  assert.equal(appStatusMarkup({ id: 'shop', run_command: '' }, 'h'), '');
});

test('SID: labelled as this system, no GitHub push form, shows its host setup', () => {
  const list = projectListMarkup([{ id: 'sid', name: 'SID', importance: 'high', status: 'active', counts: {}, stats: {} }]);
  assert.match(list, /this system/);
  const page = projectDetailMarkup({ id: 'sid', name: 'SID', status: 'active', system: { remote: 'git@github.com:me/sid.git', branch: 'main', head: 'abc1234', subject: '<b>' } });
  assert.match(page, /SID · THIS SYSTEM/);
  assert.doesNotMatch(page, /project-push-form/);
  assert.match(page, /git@github.com:me\/sid.git/);
  assert.match(page, /main @ abc1234/);
  assert.match(page, /&lt;b&gt;/);
  assert.match(page, /project-goal-form/);
  assert.match(projectDetailMarkup({ id: 'shop', status: 'active' }), /project-push-form/);
  assert.match(systemInfoMarkup(null), /not reported yet/);
});

test('trash: restorable projects with time left, nothing when empty', () => {
  const html = trashMarkup([{ trash_id: 'shop-20260930T220000Z', project_id: 'shop', name: '<Shop>', deleted_at: 1000, expires_at: 1000 + 86400 }], 1000 + 3600);
  assert.match(html, /Recently deleted/);
  assert.match(html, /data-restore-trash="shop-20260930T220000Z"/);
  assert.match(html, /removed for good in 23 hours/);
  assert.match(html, /&lt;Shop&gt;/);
  assert.equal(trashMarkup([]), '');
  assert.match(deleteProjectMarkup('shop'), /restore it from the Projects page for 1 day/);
});

test('build settings include app limits and send only what is filled in', () => {
  const html = buildSettingsMarkup({ id: 'shop', run_memory_mb: 1024, run_cpus: 1, run_tasks: 512 });
  assert.match(html, /name="run_memory_mb" value="1024"/);
  assert.match(html, /name="run_cpus" value="1"/);
  assert.deepEqual(buildSettingsRequest({ setup_command: '', gate_command: '', run_command: '', run_memory_mb: '256', run_cpus: '', run_tasks: '' }),
    { setup_command: '', gate_command: '', run_command: '', run_memory_mb: 256 });
});

test('environment: names and lengths only, never values; not for SID', async () => {
  const { envMarkup } = await import('./lib/projects.js');
  const html = envMarkup('shop', { variables: [{ name: 'STRIPE_KEY', length: 32 }] });
  assert.match(html, /<code>STRIPE_KEY<\/code>/);
  assert.match(html, /32 characters/);
  assert.match(html, /data-env-delete="STRIPE_KEY"/);
  assert.match(html, /type="password"/);
  assert.equal(envMarkup('sid', {}), '');
  assert.match(envMarkup('shop', null), /No variables yet/);
});
