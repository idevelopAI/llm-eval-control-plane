import { readdir, readFile } from 'node:fs/promises';
import { extname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const dashboardRoot = fileURLToPath(new URL('..', import.meta.url));
const distRoot = join(dashboardRoot, 'dist');
const failures = [];

function fail(message) {
  failures.push(message);
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
      ? Object.keys(value).length === 0
      : value == null;
  if (!empty) fail(`${label} must be empty in the public Site build.`);
}

const files = await walk(distRoot);
const relativeFiles = files.map((path) => relative(distRoot, path));
const textExtensions = new Set(['.css', '.html', '.js', '.json', '.mjs', '.txt']);
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
const applicationOnlyMarkers = new Set([
  'authorization header',
  'bearer credential',
]);

for (const [path, content] of textByFile) {
  if (extname(path) !== '.js') continue;
  for (const [label, pattern] of capabilityMarkers) {
    const applicationEntry = /public-release-dashboard-[^/]+\.js$/.test(path);
    if (
      pattern.test(content) &&
      (!applicationOnlyMarkers.has(label) || applicationEntry)
    ) {
      fail(`${label} found in ${relative(distRoot, path)}.`);
    }
  }
}

const clientManifestPath = join(distRoot, 'client', '.vite', 'manifest.json');
const clientManifest = await readJson(clientManifestPath);
const publicEntry = clientManifest['app/public-release-dashboard.tsx'];
if (!publicEntry?.file) {
  fail('The fixture-only public dashboard is missing from the client manifest.');
}
if (clientManifest['app/release-dashboard.tsx']) {
  fail('The local live dashboard entered the public client manifest.');
}

const publicClientChunks = files.filter((path) =>
  /client\/_next\/static\/chunks\/public-release-dashboard-[^/]+\.js$/.test(
    path,
  ),
);
const publicSsrChunks = files.filter((path) =>
  /server\/ssr\/_next\/static\/public-release-dashboard-[^/]+\.js$/.test(
    path,
  ),
);
if (publicClientChunks.length !== 1) {
  fail(`Expected one public client entry chunk; found ${publicClientChunks.length}.`);
}
if (publicSsrChunks.length !== 1) {
  fail(`Expected one public SSR entry chunk; found ${publicSsrChunks.length}.`);
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
for (const path of [...publicClientChunks, ...publicSsrChunks]) {
  const content = textByFile.get(path) ?? '';
  for (const [label, pattern] of persistenceMarkers) {
    if (pattern.test(content)) {
      fail(`${label} found in application entry ${relative(distRoot, path)}.`);
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
      fail(`${label} found in ${relative(distRoot, path)}.`);
    }
  }
}

const publicCss = textFiles.filter((path) => extname(path) === '.css');
for (const path of publicCss) {
  if (/(?:linear|radial|conic)-gradient\s*\(/i.test(textByFile.get(path) ?? '')) {
    fail(`Gradient found in ${relative(distRoot, path)}.`);
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
const emptyBindingPaths = [
  ['vars', wrangler.vars],
  ['durable_objects.bindings', wrangler.durable_objects?.bindings],
  ['kv_namespaces', wrangler.kv_namespaces],
  ['queues.producers', wrangler.queues?.producers],
  ['queues.consumers', wrangler.queues?.consumers],
  ['connect', wrangler.connect],
  ['r2_buckets', wrangler.r2_buckets],
  ['d1_databases', wrangler.d1_databases],
  ['vectorize', wrangler.vectorize],
  ['ai_search_namespaces', wrangler.ai_search_namespaces],
  ['ai_search', wrangler.ai_search],
  ['agent_memory', wrangler.agent_memory],
  ['hyperdrive', wrangler.hyperdrive],
  ['workflows', wrangler.workflows],
  ['secrets_store_secrets', wrangler.secrets_store_secrets],
  ['services', wrangler.services],
  ['analytics_engine_datasets', wrangler.analytics_engine_datasets],
  ['dispatch_namespaces', wrangler.dispatch_namespaces],
  ['send_email', wrangler.send_email],
  ['mtls_certificates', wrangler.mtls_certificates],
  ['pipelines', wrangler.pipelines],
  ['vpc_services', wrangler.vpc_services],
  ['vpc_networks', wrangler.vpc_networks],
  ['triggers', wrangler.triggers],
];
for (const [label, value] of emptyBindingPaths) assertEmpty(value, label);

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
      fail(`Server prerender secret leaked into ${relative(distRoot, path)}.`);
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
