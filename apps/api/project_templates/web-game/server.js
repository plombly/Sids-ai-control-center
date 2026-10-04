// {{NAME}}: a small web server with no dependencies.
// LAIka runs it with PORT set (and HOST=0.0.0.0); open it at http://<server>:<port>/.
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, normalize, resolve } from 'node:path';

const PUBLIC = resolve(new URL('./public', import.meta.url).pathname);
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json; charset=utf-8', '.ico': 'image/x-icon' };

export function createServer() {
  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      if (url.pathname === '/health') return send(res, 200, { status: 'ok' });
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
