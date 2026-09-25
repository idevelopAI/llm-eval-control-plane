import assert from 'node:assert/strict';
import { test } from 'node:test';
import { assertPublicPath, assertPublicText, parseClientManifest, staticHostingConfig } from './static-demo-policy.mjs';

test('static routing denies APIs and all writes before filesystem serving', () => {
  const config = staticHostingConfig();
  assert.equal(config.framework, null);
  assert.equal(config.buildCommand, null);
  assert.deepEqual(Object.keys(config).sort(), ['buildCommand', 'framework', 'outputDirectory', 'routes', 'version']);
  assert.equal(config.routes[0].headers['X-Frame-Options'], 'DENY');
  assert.equal(config.routes[1].status, 404);
  assert.ok(new RegExp(`^${config.routes[1].src}$`).test('/v1/suite-runs'));
  assert.equal(config.routes[2].status, 405);
  assert.ok(config.routes[2].methods.includes('POST'));
  assert.equal(config.routes.at(-1).status, 404);
  assert.equal(config.routes[3].headers['Cache-Control'], 'private, no-store, max-age=0');
});
test('public file allowlist excludes secrets, source, functions, and source maps', () => {
  for (const file of ['index.html', '_not-found/__next._not-found.__PAGE__.txt', '_next/static/chunks/app/page-ab123.js', '_next/static/media/font.woff2', 'vercel.json']) assertPublicPath(file);
  for (const file of ['.env', '.env.local', 'functions/api.js', 'app/page.tsx', '_next/static/page.js.map', '.git/config', 'out/index.html']) assert.throws(() => assertPublicPath(file));
});
test('rejects local write controls, provider endpoints, and credential markers', () => {
  for (const text of ['Run write credential', 'Use local live data', 'Idempotency-Key', 'X-Project-ID', 'api.openai.com', `cpk_${'x'.repeat(43)}`]) assert.throws(() => assertPublicText('page.js', text));
});
test('rejects old hosting metadata and gradients', () => {
  for (const text of ['https://example.chatgpt.site', 'linear-gradient(red, blue)', '<radialGradient>']) assert.throws(() => assertPublicText('index.html', text));
});
test('application chunks cannot request data or persist browser state', () => {
  for (const text of ['fetch("/something")', 'new WebSocket()', 'localStorage.setItem()', 'document.cookie', 'sendBeacon()', '"form"']) assert.throws(() => assertPublicText('page.js', text, true));
  assertPublicText('page.js', 'Synthetic release evidence', true);
});
test('client manifest is parsed as data, never evaluated', () => {
  const prefix = 'globalThis.__RSC_MANIFEST=(globalThis.__RSC_MANIFEST||{});globalThis.__RSC_MANIFEST["/page"]=';
  assert.deepEqual(parseClientManifest(`${prefix}{"clientModules":{}};`), { clientModules: {} });
  assert.throws(() => parseClientManifest(`${prefix}process.exit(0)`));
  assert.throws(() => parseClientManifest('unexpected()'));
});
