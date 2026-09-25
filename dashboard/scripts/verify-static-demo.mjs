import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { extname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SITE_ORIGIN, SOCIAL_PREVIEW_URL } from '../src/site-metadata.ts';
import { assertPublicPath, assertPublicText, parseClientManifest, staticHostingConfig } from './static-demo-policy.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const json = async (path) => JSON.parse(await readFile(join(root, path), 'utf8'));

export async function verifyStaticDemo() {
  const config = await json('.next/required-server-files.json');
  assert.equal(config.config.output, 'export', 'Deployment must be a static export');
  const routes = await json('.next/server/app-paths-manifest.json');
  assert.deepEqual(Object.keys(routes).sort(), ['/_global-error/page', '/_not-found/page', '/page']);
  const prerender = await json('.next/prerender-manifest.json');
  assert.deepEqual(Object.keys(prerender.dynamicRoutes), []);
  const actions = await json('.next/server/server-reference-manifest.json');
  assert.deepEqual(Object.keys(actions.node), []);
  assert.deepEqual(Object.keys(actions.edge), []);
  assert.deepEqual(await json('out/vercel.json'), staticHostingConfig());

  const manifest = parseClientManifest(await readFile(join(root, '.next/server/app/page_client-reference-manifest.js'), 'utf8'));
  const ownModules = Object.entries(manifest.clientModules).filter(([path]) => !path.includes('/node_modules/'));
  assert.deepEqual(ownModules.map(([path]) => relative(root, path).split(sep).join('/')).sort(), ['app/globals.css', 'app/public-release-dashboard.tsx']);
  const appChunks = new Set(ownModules.flatMap(([, value]) => value.chunks.filter((chunk) => chunk.endsWith('.js')).map((chunk) => `_next/${chunk}`)));
  assert.ok(appChunks.size > 0);
  let count = 0;
  let bytes = 0;
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = join(directory, entry.name);
      assert.ok(!entry.isSymbolicLink(), 'No symlinks in the deployment');
      if (entry.isDirectory()) { await walk(file); continue; }
      assert.ok(entry.isFile(), 'Only regular static files may be deployed');
      const name = relative(join(root, 'out'), file).split(sep).join('/');
      assertPublicPath(name);
      const content = await readFile(file);
      count++; bytes += content.byteLength;
      if (['.html', '.js', '.css', '.json', '.txt', '.svg'].includes(extname(file)) && name !== 'vercel.json') {
        assertPublicText(name, content.toString('utf8'), appChunks.has(name));
      }
    }
  }
  await walk(join(root, 'out'));
  const html = await readFile(join(root, 'out/index.html'), 'utf8');
  assert.ok(html.includes('Public example environment') && html.includes('gate-ledger'), 'Missing prerendered evidence');
  const canonical = html.match(/<link rel="canonical" href="([^"]+)"/)?.[1];
  assert.equal(new URL(canonical ?? 'about:blank').toString(), SITE_ORIGIN.toString(), 'Wrong canonical URL');
  assert.ok(html.includes(SOCIAL_PREVIEW_URL), 'Wrong social image origin');
  assert.ok(bytes < 100 * 1024 * 1024, 'Static deployment exceeds the Hobby file allowance');
  console.log(`Static demo verified: ${count} files, ${bytes} bytes; fixture-only, no functions or secrets, no gradients.`);
}
