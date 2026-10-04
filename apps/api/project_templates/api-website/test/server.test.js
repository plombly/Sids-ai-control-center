import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from '../server.js';

async function start() {
  const server = createServer();
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  return { server, base: `http://127.0.0.1:${server.address().port}` };
}

test('serves the page and answers /health', async () => {
  const { server, base } = await start();
  try {
    const page = await fetch(`${base}/`);
    assert.equal(page.status, 200);
    assert.match(page.headers.get('content-type'), /text\/html/);
    assert.deepEqual(await (await fetch(`${base}/health`)).json(), { status: 'ok' });
    assert.equal((await fetch(`${base}/..%2fpackage.json`)).status, 404);
  } finally {
    server.close();
  }
});

test('notes can be added and listed, and bad input is refused', async () => {
  const { mkdtemp } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  process.env.DATA_DIR = await mkdtemp(`${tmpdir()}/notes-`);
  const { server, base } = await start();
  try {
    const made = await fetch(`${base}/api/notes`, { method: 'POST', body: JSON.stringify({ text: 'hello' }) });
    assert.equal(made.status, 201);
    assert.equal((await (await fetch(`${base}/api/notes`)).json())[0].text, 'hello');
    assert.equal((await fetch(`${base}/api/notes`, { method: 'POST', body: '{"text": ""}' })).status, 400);
  } finally {
    server.close();
  }
});
