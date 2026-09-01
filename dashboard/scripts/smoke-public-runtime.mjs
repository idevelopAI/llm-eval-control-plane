import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const dashboardRoot = fileURLToPath(new URL('..', import.meta.url));
const vinextCli = join(
  dashboardRoot,
  'node_modules',
  'vinext',
  'dist',
  'cli.js',
);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function reservePort() {
  const server = createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert(address && typeof address === 'object', 'Could not reserve a port.');
  const port = address.port;
  server.close();
  await once(server, 'close');
  return port;
}

async function waitForRuntime(origin, processExited) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (processExited()) throw new Error('Production runtime exited before readiness.');
    try {
      const response = await fetch(origin, { redirect: 'manual' });
      if (response.status === 200) return response;
    } catch {
      // The server has not opened its listener yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('Production runtime did not become ready within 8 seconds.');
}

const port = await reservePort();
const origin = `http://127.0.0.1:${port}/`;
let output = '';
let exitCode;
const child = spawn(
  process.execPath,
  [vinextCli, 'start', '--hostname', '127.0.0.1', '--port', String(port)],
  {
    cwd: dashboardRoot,
    env: { ...process.env, NODE_ENV: 'production' },
    stdio: ['ignore', 'pipe', 'pipe'],
  },
);
child.stdout.on('data', (chunk) => {
  output += chunk.toString();
});
child.stderr.on('data', (chunk) => {
  output += chunk.toString();
});
child.on('exit', (code) => {
  exitCode = code ?? 1;
});

try {
  const root = await waitForRuntime(origin, () => exitCode !== undefined);
  const html = await root.text();
  const csp = root.headers.get('content-security-policy') ?? '';
  const cacheControl = root.headers.get('cache-control') ?? '';

  assert(html.includes('Public example environment'), 'Public fixture copy is missing.');
  assert(!html.includes('Use local live data'), 'Local live UI reached production HTML.');
  assert(!html.includes('Read-only access token'), 'Credential UI reached production HTML.');
  assert(cacheControl.includes('private'), 'Root response must be private.');
  assert(cacheControl.includes('no-store'), 'Root response must be non-cacheable.');
  assert(csp.includes("connect-src 'self'"), 'CSP connect boundary is missing.');
  assert(csp.includes("frame-ancestors 'none'"), 'CSP frame boundary is missing.');
  assert(csp.includes("object-src 'none'"), 'CSP object boundary is missing.');
  assert(root.headers.get('x-frame-options') === 'DENY', 'Frame denial is missing.');
  assert(
    root.headers.get('x-content-type-options') === 'nosniff',
    'MIME-sniffing protection is missing.',
  );
  assert(
    root.headers.get('referrer-policy') === 'no-referrer',
    'Referrer suppression is missing.',
  );

  const methods = ['GET', 'POST', 'HEAD', 'OPTIONS'];
  const paths = ['/api/public-build-probe', '/v1/public-build-probe'];
  for (const path of paths) {
    for (const method of methods) {
      const response = await fetch(new URL(path, origin), {
        method,
        redirect: 'manual',
      });
      assert(
        response.status === 404,
        `${method} ${path} unexpectedly returned ${response.status}.`,
      );
    }
  }

  console.log(
    'Public runtime verified: hardened fixture HTML and no /api or /v1 handlers.',
  );
} catch (error) {
  if (output.trim()) console.error(output.trim());
  throw error;
} finally {
  if (exitCode === undefined) child.kill('SIGTERM');
  if (exitCode === undefined) await once(child, 'exit');
}
