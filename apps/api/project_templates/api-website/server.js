// {{NAME}}: a small web server with no dependencies.
// LAIka runs it with PORT set (and HOST=0.0.0.0); open it at http://<server>:<port>/.
import http from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';

const PUBLIC = resolve(new URL('./public', import.meta.url).pathname);
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json; charset=utf-8', '.ico': 'image/x-icon' };

// Notes, kept in DATA_DIR/notes.json (LAIka gives every app its own data folder).
const dataDir = () => process.env.DATA_DIR || resolve('data');

async function loadNotes() {
  try {
    return JSON.parse(await readFile(join(dataDir(), 'notes.json'), 'utf8'));
  } catch {
    return [];
  }
}

async function saveNotes(notes) {
  await mkdir(dataDir(), { recursive: true });
  await writeFile(join(dataDir(), 'notes.json'), JSON.stringify(notes, null, 1));
}

async function readJson(req) {
  let body = '';
  for await (const chunk of req) {
    body += chunk;
    if (body.length > 100000) throw new Error('too large');
  }
  return JSON.parse(body || '{}');
}

export async function api(req, res, url) {
  if (url.pathname === '/api/notes' && req.method === 'GET') return send(res, 200, await loadNotes());
  if (url.pathname === '/api/notes' && req.method === 'POST') {
    let input;
    try {
      input = await readJson(req);
    } catch {
      return send(res, 400, { error: 'send JSON like {"text": "..."}' });
    }
    const text = String(input.text || '').trim();
    if (!text || text.length > 500) return send(res, 400, { error: 'text must be 1-500 characters' });
    const notes = await loadNotes();
    const note = { id: Date.now().toString(36), text, created_at: new Date().toISOString() };
    await saveNotes([note, ...notes]);
    return send(res, 201, note);
  }
  return send(res, 404, { error: 'unknown API route' });
}

export function createServer() {
  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      if (url.pathname === '/health') return send(res, 200, { status: 'ok' });
      if (url.pathname.startsWith('/api/')) return await api(req, res, url);
      // Static files from public/, never outside it.
      const file = resolve(PUBLIC, '.' + normalize(decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname)));
      if (!file.startsWith(PUBLIC + '/')) return send(res, 404, { error: 'not found' });
      const body = await readFile(file);
      res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
      res.end(body);
    } catch {
      send(res, 404, { error: 'not found' });
    }
  });
}

function send(res, status, data) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(data));
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  const port = Number(process.env.PORT) || 3000;
  createServer().listen(port, process.env.HOST || '0.0.0.0', () => console.log(`{{NAME}} listening on port ${port}`));
}
