import { readdir, readFile } from 'node:fs/promises';
import { extname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const dashboardRoot = fileURLToPath(new URL('..', import.meta.url));
const distRoot = join(dashboardRoot, 'dist');
const failures = [];

function fail(message) {
  failures.push(message);
}

function portableRelative(path) {
  return relative(distRoot, path).split(sep).join('/');
}

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await walk(path)));
    if (entry.isFile()) files.push(path);
  }
  return files;
}

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

function assertEmpty(value, label) {
  const empty = Array.isArray(value)
    ? value.length === 0
    : value != null && typeof value === 'object'
      ? Object.values(value).every((nested) => {
          if (Array.isArray(nested)) return nested.length === 0;
          if (nested != null && typeof nested === 'object') {
            return Object.keys(nested).length === 0;
          }
          return nested == null;
        })
      : value == null;
  if (!empty) fail(`${label} must be empty in the public Site build.`);
}

const files = await walk(distRoot);
const relativeFiles = files.map(portableRelative);
const textExtensions = new Set([
  '.css',
  '.html',
  '.js',
  '.json',
  '.mjs',
  '.svg',
  '.txt',
  '.xml',
]);
const textFiles = files.filter((path) => textExtensions.has(extname(path)));
const textByFile = new Map(
  await Promise.all(
    textFiles.map(async (path) => [path, await readFile(path, 'utf8')]),
  ),
);

const forbiddenFile = /(^|\/)(?:\.env(?:\.|$)|credentials?(?:\.|$))|\.(?:crt|key|map|pem|p12|pfx)$/i;
for (const path of relativeFiles) {
  if (forbiddenFile.test(path)) fail(`Forbidden build artifact: ${path}`);
}

const clientManifestPath = join(distRoot, 'client', '.vite', 'manifest.json');
const clientManifest = await readJson(clientManifestPath);
const publicEntryKey = 'app/public-release-dashboard.tsx';
const publicEntry = clientManifest[publicEntryKey];
if (!publicEntry?.file) {
  fail('The fixture-only public dashboard is missing from the client manifest.');
}
if (clientManifest['app/release-dashboard.tsx']) {
  fail('The local live dashboard entered the public client manifest.');
}

function reachableManifestKeys(startKey) {
  const reachable = new Set();
  const pending = [startKey];
  while (pending.length > 0) {
    const key = pending.pop();
    if (!key || reachable.has(key)) continue;
    const entry = clientManifest[key];
    if (!entry) {
      fail(`Client manifest dependency ${key} could not be classified.`);
      continue;
    }
    reachable.add(key);
    pending.push(...(entry.imports ?? []), ...(entry.dynamicImports ?? []));
  }
  return reachable;
}

const reachableKeys = publicEntry ? reachableManifestKeys(publicEntryKey) : new Set();
const reachableApplicationKeys = [...reachableKeys].filter((key) => {
  const source = clientManifest[key]?.src ?? key;
  return /^(?:app|src)\//.test(source);
});
if (
  reachableApplicationKeys.some((key) => {
    const source = clientManifest[key]?.src ?? key;
    return source === 'app/release-dashboard.tsx';
  })
) {
  fail('The local live dashboard is reachable from the public client entry.');
}

const reachableApplicationClientChunks = reachableApplicationKeys
  .map((key) => clientManifest[key]?.file)
  .filter((file) => typeof file === 'string')
  .map((file) => join(distRoot, 'client', file));

const publicClientChunks = reachableApplicationClientChunks.filter((path) =>
  /client\/_next\/static\/chunks\/public-release-dashboard-[^/]+\.js$/.test(
    portableRelative(path),
  ),
);
const publicSsrChunks = files.filter((path) =>
  /server\/ssr\/_next\/static\/public-release-dashboard-[^/]+\.js$/.test(
    portableRelative(path),
  ),
);
if (publicClientChunks.length !== 1) {
  fail(`Expected one public client entry chunk; found ${publicClientChunks.length}.`);
}
if (publicSsrChunks.length !== 1) {
  fail(`Expected one public SSR entry chunk; found ${publicSsrChunks.length}.`);
}

const ssrChunkPrefix = 'server/ssr/_next/static/';
const knownFrameworkSsrChunk = /^(?:app-prefetch-fetch-queue|app-router-scroll|error-boundary|layout-segment-context|react|static\.edge|streamed-icons)-[^/]+\.js$/;
const unknownSsrChunks = files
  .map((path) => [path, portableRelative(path)])
  .filter(([, path]) => path.startsWith(ssrChunkPrefix))
  .filter(([, path]) => {
    const name = path.slice(ssrChunkPrefix.length);
    return (
      !knownFrameworkSsrChunk.test(name) &&
      !/^public-release-dashboard-[^/]+\.js$/.test(name)
    );
  });
for (const [, path] of unknownSsrChunks) {
  fail(`Unclassified public SSR chunk: ${path}`);
}

const applicationChunks = [
  ...new Set([...reachableApplicationClientChunks, ...publicSsrChunks]),
];

const capabilityMarkers = [
  ['control-plane API route', /\/v1\//i],
  ['authorization header', /\bauthorization\b/i],
  ['bearer credential', /\bbearer\b/i],
  ['project credential header', /x-project-id/i],
  ['control-plane key format', /cpk_/i],
  ['credential entry UI', /read-only access token/i],
  ['live-mode UI', /use local live data/i],
  ['model provider SDK', /(?:openai|anthropic)[-_/](?:sdk|client)|@(?:anthropic-ai|google)\//i],
  ['model endpoint', /api\.(?:openai|anthropic)\.com|generativelanguage\.googleapis\.com/i],
];
const requestMarkers = [
  ['fetch', /\bfetch\s*\(/],
  ['XMLHttpRequest', /\bXMLHttpRequest\b/],
  ['WebSocket', /\bWebSocket\b/],
  ['EventSource', /\bEventSource\b/],
  ['background beacon', /\bsendBeacon\b/],
  ['image beacon', /\bnew\s+Image\s*\(|document\.createElement\(["'`]img["'`]\)/],
  ['form submission element', /["'`]form["'`]/],
];

for (const path of applicationChunks) {
  const content = textByFile.get(path) ?? '';
  for (const [label, pattern] of [...capabilityMarkers, ...requestMarkers]) {
    if (pattern.test(content)) {
      fail(`${label} found in application chunk ${portableRelative(path)}.`);
    }
  }
}

const persistenceMarkers = [
  ['local storage', /\blocalStorage\b/],
  ['session storage', /\bsessionStorage\b/],
  ['IndexedDB', /\bindexedDB\b/],
  ['cookie write', /document\.cookie/],
  ['background beacon', /\bsendBeacon\b/],
  ['WebSocket', /\bWebSocket\b/],
  ['EventSource', /\bEventSource\b/],
];
for (const path of applicationChunks) {
  const content = textByFile.get(path) ?? '';
  for (const [label, pattern] of persistenceMarkers) {
    if (pattern.test(content)) {
      fail(`${label} found in application chunk ${portableRelative(path)}.`);
    }
  }
}

const secretPatterns = [
  ['private key', /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/],
  ['OpenAI-style key', /\bsk-[A-Za-z0-9_-]{20,}\b/],
  ['GitHub token', /\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b/],
  ['AWS access key', /\bAKIA[0-9A-Z]{16}\b/],
  ['database URL', /\b(?:postgres(?:ql)?|mysql):\/\/[A-Za-z0-9]/i],
];
for (const [path, content] of textByFile) {
  for (const [label, pattern] of secretPatterns) {
    if (pattern.test(content)) {
      fail(`${label} found in ${portableRelative(path)}.`);
    }
  }
}

const gradientFiles = textFiles.filter((path) =>
  new Set(['.css', '.html', '.js', '.svg', '.xml']).has(extname(path)),
);
for (const path of gradientFiles) {
  const content = textByFile.get(path) ?? '';
  if (
    /(?:linear|radial|conic)-gradient\s*\(/i.test(content) ||
    /<(?:linearGradient|radialGradient)\b/i.test(content)
  ) {
    fail(`Gradient found in ${portableRelative(path)}.`);
  }
}

const hosting = await readJson(join(distRoot, '.openai', 'hosting.json'));
if (hosting.d1 !== null || hosting.r2 !== null) {
  fail('The public Site must not declare D1 or R2 resources.');
}
const hostingKeys = Object.keys(hosting).sort();
if (hostingKeys.join(',') !== 'd1,project_id,r2') {
  fail(`Unexpected hosting keys: ${hostingKeys.join(', ')}`);
}

const wrangler = await readJson(join(distRoot, 'server', 'wrangler.json'));
const allowedNonBindingKeys = new Set([
  'assets',
  'build',
  'compatibility_date',
  'compatibility_flags',
  'dev',
  'jsx_factory',
  'jsx_fragment',
  'main',
  'name',
  'no_bundle',
  'observability',
  'python_modules',
  'rules',
  'topLevelName',
]);
for (const [key, value] of Object.entries(wrangler)) {
  if (!allowedNonBindingKeys.has(key)) assertEmpty(value, `wrangler.${key}`);
}

if (wrangler.main !== 'index.js' || wrangler.no_bundle !== true) {
  fail('The public Worker entry must be the unbundled generated index.js.');
}
if (wrangler.assets?.directory !== '../client') {
  fail('The public Worker assets directory is unexpected.');
}
if (
  !Array.isArray(wrangler.compatibility_flags) ||
  wrangler.compatibility_flags.join(',') !== 'nodejs_compat'
) {
  fail('Unexpected Worker compatibility flags.');
}
const devKeys = Object.keys(wrangler.dev ?? {}).sort().join(',');
if (devKeys !== 'enable_containers,generate_types,ip,local_protocol,upstream_protocol') {
  fail(`Unexpected public Worker dev keys: ${devKeys}`);
}
const buildKeys = Object.keys(wrangler.build ?? {}).sort().join(',');
if (buildKeys !== 'watch_dir') fail(`Unexpected Worker build keys: ${buildKeys}`);
const observabilityKeys = Object.keys(wrangler.observability ?? {}).sort().join(',');
if (observabilityKeys !== 'enabled') {
  fail(`Unexpected Worker observability keys: ${observabilityKeys}`);
}
const pythonModuleKeys = Object.keys(wrangler.python_modules ?? {}).sort().join(',');
if (pythonModuleKeys !== 'exclude') {
  fail(`Unexpected Worker Python module keys: ${pythonModuleKeys}`);
}
if (
  !Array.isArray(wrangler.rules) ||
  wrangler.rules.length !== 1 ||
  wrangler.rules[0]?.type !== 'ESModule' ||
  wrangler.rules[0]?.globs?.join(',') !== '**/*.js,**/*.mjs'
) {
  fail('Unexpected public Worker module rules.');
}

const serverManifest = await readJson(
  join(distRoot, 'server', '.vite', 'manifest.json'),
);
const routeEntries = Object.keys(serverManifest).filter((key) =>
  /(?:^|\/)route\.[cm]?[jt]sx?$/.test(key),
);
if (routeEntries.length > 0) {
  fail(`Unexpected application route handlers: ${routeEntries.join(', ')}`);
}

const prerenderManifest = await readJson(
  join(distRoot, 'server', 'vinext-server.json'),
);
const prerenderSecret = prerenderManifest.prerenderSecret;
if (typeof prerenderSecret !== 'string' || prerenderSecret.length < 32) {
  fail('Expected the framework prerender secret in the server-only manifest.');
} else {
  const clientFiles = [...textByFile.entries()].filter(([path]) =>
    path.startsWith(join(distRoot, 'client')),
  );
  for (const [path, content] of clientFiles) {
    if (content.includes(prerenderSecret)) {
      fail(`Server prerender secret leaked into ${portableRelative(path)}.`);
    }
  }
}

if (failures.length > 0) {
  console.error('Public build verification failed:');
  for (const failure of failures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log(
    'Public build verified: fixture-only, request-free, binding-free, secret-free, and gradient-free.',
  );
}
