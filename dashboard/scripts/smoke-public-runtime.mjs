import assert from 'node:assert/strict';
import { once } from 'node:events';
import { startStaticDemoServer } from './serve-static-demo.mjs';
import { PRODUCTION_SECURITY_HEADERS, PRIVATE_RESPONSE_HEADERS } from '../src/security/production-headers.ts';

const server = await startStaticDemoServer(0);
const origin = `http://127.0.0.1:${server.address().port}`;
const request = async (path, method = 'GET') => {
  const response = await fetch(origin + path, { method, redirect: 'manual', signal: AbortSignal.timeout(2_000) });
  for (const { key, value } of [...PRODUCTION_SECURITY_HEADERS, ...PRIVATE_RESPONSE_HEADERS]) {
    assert.equal(response.headers.get(key), value, `${method} ${path}: ${key}`);
  }
  assert.equal(response.headers.get('set-cookie'), null);
  assert.equal(response.headers.get('access-control-allow-origin'), null);
  return response;
};
try {
  const root = await request('/');
  assert.equal(root.status, 200);
  const html = await root.text();
  assert.ok(html.includes('Public example environment'));
  assert.ok(!/Use local live data|Read-only access token|Run write credential/.test(html));
  for (const path of ['/api', '/api/probe', '/v1', '/v1/probe']) {
    for (const method of ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']) {
      assert.equal((await request(path, method)).status, 404, `${method} ${path}`);
    }
  }
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) assert.equal((await request('/', method)).status, 405);
  for (const path of ['/missing', '/.env', '/vercel.json', '/_next/static/%2e%2e%2f%2e%2e%2fpackage.json', '/_next/static/%5cpackage.json']) assert.equal((await request(path)).status, 404, path);
  assert.equal((await request('/%ZZ')).status, 400);
  const assets = [...new Set([...html.matchAll(/(?:src|href)="([^"#]+)"/g)].map((match) => match[1]).filter((path) => path.startsWith('/_next/') || path === '/favicon.svg'))];
  for (const path of [...assets, '/og.png']) assert.equal((await request(path)).status, 200, path);
  assert.equal(await (await request('/', 'HEAD')).text(), '');
  console.log('Static preview verified: fixture HTML, security headers, assets, denied APIs/writes, and no path traversal. Vercel CDN behavior is checked separately.');
} finally {
  const closed = once(server, 'close');
  server.close(); server.closeAllConnections();
  await closed;
}
