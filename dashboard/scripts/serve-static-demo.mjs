import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PRODUCTION_SECURITY_HEADERS, PRIVATE_RESPONSE_HEADERS } from '../src/security/production-headers.ts';
import { assertPublicPath } from './static-demo-policy.mjs';
import { verifyStaticDemo } from './verify-static-demo.mjs';

const root = fileURLToPath(new URL('../out/', import.meta.url));
const types = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff': 'font/woff',
  '.woff2': 'font/woff2', '.ttf': 'font/ttf',
};

/** Local preview only. This server is never part of the uploaded static export. */
export async function startStaticDemoServer(port = 3000) {
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid preview port');
  await verifyStaticDemo();
  const server = createServer(async (request, response) => {
    for (const { key, value } of PRODUCTION_SECURITY_HEADERS) response.setHeader(key, value);
    for (const { key, value } of PRIVATE_RESPONSE_HEADERS) response.setHeader(key, value);
    let path;
    try { path = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname); }
    catch { response.writeHead(400); response.end(); return; }
    if (/^\/(?:api|v1)(?:\/|$)/.test(path)) {
      response.writeHead(404); response.end(); return;
    }
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.writeHead(405, { Allow: 'GET, HEAD' }); response.end(); return;
    }
    const file = path === '/' ? 'index.html' : path.slice(1);
    try {
      if (file === 'vercel.json' || file.includes('\\') || file.split('/').some((part) => part === '.' || part === '..')) throw new Error('Not a public asset');
      assertPublicPath(file);
      const content = await readFile(join(root, file));
      response.writeHead(200, { 'Content-Type': types[extname(file)] ?? 'application/octet-stream', 'Content-Length': content.length });
      response.end(request.method === 'HEAD' ? undefined : content);
    } catch {
      const content = await readFile(join(root, '404.html')).catch(() => Buffer.from('Not found'));
      response.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' });
      response.end(request.method === 'HEAD' ? undefined : content);
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const server = await startStaticDemoServer(Number(process.env.PORT ?? 3000));
  console.log(`Static demo preview: http://127.0.0.1:${server.address().port}`);
  const close = () => { server.close(); server.closeAllConnections(); };
  process.once('SIGINT', close);
  process.once('SIGTERM', close);
}
