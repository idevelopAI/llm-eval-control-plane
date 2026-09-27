import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const requests = [];
const backend = createServer(async (request, response) => {
  let body = '';
  for await (const chunk of request) body += chunk;
  requests.push({
    path: request.url, method: request.method, body,
    authorization: request.headers.authorization,
    project: request.headers['x-project-id'],
    idempotency: request.headers['idempotency-key'],
  });
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ fixture: true }));
});
backend.listen(0, '127.0.0.1');
await once(backend, 'listening');
const reservation = createServer();
reservation.listen(0, '127.0.0.1');
await once(reservation, 'listening');
const port = reservation.address().port;
reservation.close(); await once(reservation, 'close');
const origin = `http://127.0.0.1:${port}`;
const child = spawn(process.execPath, ['node_modules/next/dist/bin/next', 'dev', '--webpack', '--hostname', '127.0.0.1', '--port', String(port)], {
  cwd: root,
  env: { ...process.env, NEXT_TELEMETRY_DISABLED: '1', CONTROL_PLANE_DEV_ORIGIN: `http://127.0.0.1:${backend.address().port}` },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let output = '';
child.stdout.on('data', (chunk) => { output = (output + chunk).slice(-8_000); });
child.stderr.on('data', (chunk) => { output = (output + chunk).slice(-8_000); });
const request = (path, options = {}) => fetch(origin + path, { ...options, signal: AbortSignal.timeout(60_000) });
try {
  let ready = false;
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline && child.exitCode === null) {
    try { const response = await request('/'); ready = response.status === 200; await response.arrayBuffer(); if (ready) break; }
    catch { /* Await the loopback listener and initial compilation. */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.ok(ready, 'Next development server did not become ready');
  assert.equal(requests.length, 0, 'Initial fixture render must not call the API');
  for (const path of ['/health/ready', '/openapi.json', '/v1/probe?limit=1']) assert.equal((await request(path)).status, 200, path);
  // Deliberately unusable fixture value, never a real API credential.
  assert.equal((await request('/v1/probe', {
    method: 'POST',
    headers: {
      Authorization: 'fixture-only', 'Content-Type': 'application/json',
      'X-Project-ID': 'synthetic-proxy-test', 'Idempotency-Key': 'synthetic-proxy-test',
    },
    body: '{"fixture":true}',
  })).status, 200);
  assert.deepEqual(requests.map(({ path, method }) => ({ path, method })), [
    { path: '/health/ready', method: 'GET' }, { path: '/openapi.json', method: 'GET' },
    { path: '/v1/probe?limit=1', method: 'GET' }, { path: '/v1/probe', method: 'POST' },
  ]);
  assert.equal(requests[3].authorization, 'fixture-only');
  assert.equal(requests[3].body, '{"fixture":true}');
  assert.equal(requests[3].project, 'synthetic-proxy-test');
  assert.equal(requests[3].idempotency, 'synthetic-proxy-test');
  assert.equal((await request('/api/probe')).status, 404);
  assert.equal(requests.length, 4, 'Unlisted routes must not reach the API');
  const html = await (await request('/')).text();
  const chunks = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((match) => match[1]).filter((path) => path.includes('/app/page'));
  assert.ok(chunks.length > 0, 'Missing development page chunk');
  const code = (await Promise.all(chunks.map(async (path) => (await request(path.replaceAll('&amp;', '&'))).text()))).join('\n');
  assert.ok(code.includes('Use local live data'), 'Local dashboard substitution did not run');
  console.log('Local Next development verified: live entry, loopback-only rewrite targets, exact read/write forwarding, and no initial API request.');
} catch (error) {
  console.error(output);
  throw error;
} finally {
  if (child.exitCode === null) {
    const exited = once(child, 'exit'); child.kill('SIGTERM');
    const timer = setTimeout(() => child.kill('SIGKILL'), 5_000);
    await exited; clearTimeout(timer);
  }
  const closed = once(backend, 'close'); backend.close(); backend.closeAllConnections(); await closed;
}
