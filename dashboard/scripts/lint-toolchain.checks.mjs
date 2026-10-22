import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';

const dashboard = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const nextRequire = createRequire(require.resolve('eslint-config-next'));
const pluginEntry = nextRequire.resolve('@next/eslint-plugin-next');
const pluginRequire = createRequire(pluginEntry);
const plugin = pluginRequire(pluginEntry);
const { getRootDirs } = pluginRequire('./utils/get-root-dirs.js');
const rootError = /only supports the ESLint working directory/;

test('the pinned plugin keeps default single-project discovery unchanged', () => {
  assert.equal(pluginRequire('../package.json').version, '16.4.2');
  for (const settings of [{}, { next: {} }, { next: { rootDir: undefined } }]) {
    assert.deepEqual(getRootDirs({ cwd: dashboard, settings }), [dashboard]);
  }
});

test('custom roots fail closed instead of silently skipping Next lint rules', () => {
  const nested = '{'.repeat(4000) + 'app' + '}'.repeat(4000);
  for (const rootDir of [dashboard, 'apps/*', nested, [], ['apps/*'], '', null, 1, {}]) {
    assert.throws(
      () => getRootDirs({ cwd: dashboard, settings: { next: { rootDir } } }),
      rootError,
    );
  }
});

test('the vulnerable glob dependency chain is absent, not audit-ignored', () => {
  const lockfile = readFileSync(join(dashboard, 'pnpm-lock.yaml'), 'utf8');
  for (const dependency of ['fast-glob', 'micromatch', 'braces']) {
    assert.equal(new RegExp(`^  ${dependency}@`, 'm').test(lockfile), false, `${dependency} must not be installed`);
    assert.throws(() => pluginRequire.resolve(dependency), { code: 'MODULE_NOT_FOUND' });
  }
  const workflow = readFileSync(join(dashboard, '../.github/workflows/dashboard-gate.yml'), 'utf8');
  assert.match(workflow, /run: pnpm audit --audit-level=high\s/);
});

test('all recommended Next rules remain enabled and internal links are checked', async () => {
  const fixture = mkdtempSync(join(tmpdir(), 'llm-eval-lint-'));
  try {
    mkdirSync(join(fixture, 'app'));
    mkdirSync(join(fixture, 'pages'));
    writeFileSync(join(fixture, 'app', 'page.tsx'), 'export default function Page() { return null; }');
    writeFileSync(join(fixture, 'pages', 'destination.tsx'), 'export default function Page() { return null; }');
    const eslint = new ESLint({
      cwd: fixture,
      overrideConfigFile: join(dashboard, 'eslint.config.mjs'),
    });
    const filePath = join(fixture, 'app', 'page.tsx');
    const config = await eslint.calculateConfigForFile(filePath);
    for (const rule of Object.keys(plugin.configs.recommended.rules)) {
      assert.ok(config.rules[rule][0] > 0, `${rule} must remain enabled`);
    }
    const [invalid] = await eslint.lintText(
      'export default function Page() { return <><a href="/">Home</a><a href="/destination">Go</a></>; }',
      { filePath },
    );
    assert.equal(invalid.messages.filter((item) => item.ruleId === '@next/next/no-html-link-for-pages').length, 2);
    const [valid] = await eslint.lintText(
      'import Link from "next/link"; export default function Page() { return <Link href="/destination">Go</Link>; }',
      { filePath },
    );
    assert.equal(valid.errorCount, 0);
    assert.equal(valid.warningCount, 0);

    const unsupported = new ESLint({
      cwd: fixture,
      overrideConfigFile: join(dashboard, 'eslint.config.mjs'),
      overrideConfig: { settings: { next: { rootDir: 'apps/*' } } },
    });
    await assert.rejects(
      unsupported.lintText('export default function Page() { return null; }', { filePath }),
      rootError,
    );
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});
