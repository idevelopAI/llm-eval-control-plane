import { PRODUCTION_SECURITY_HEADERS, PRIVATE_RESPONSE_HEADERS } from '../src/security/production-headers.ts';

/** CDN routing only: no functions, middleware, rewrites to external services, or API. */
export function staticHostingConfig() {
  return {
    version: 2,
    framework: null,
    buildCommand: null,
    outputDirectory: '.',
    routes: [
      { src: '/.*', headers: Object.fromEntries(PRODUCTION_SECURITY_HEADERS.map(({ key, value }) => [key, value])), continue: true },
      { src: '/(?:api|v1)(?:/.*)?', status: 404, headers: Object.fromEntries(PRIVATE_RESPONSE_HEADERS.map(({ key, value }) => [key, value])) },
      { src: '/.*', methods: ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT'], status: 405, headers: { Allow: 'GET, HEAD' } },
      { src: '/(?:index\\.html)?', headers: Object.fromEntries(PRIVATE_RESPONSE_HEADERS.map(({ key, value }) => [key, value])), continue: true },
      { src: '/', dest: '/index.html' },
      { handle: 'filesystem' },
      { src: '/.*', dest: '/404.html', status: 404 },
    ],
  };
}

export function parseClientManifest(text) {
  const prefix = 'globalThis.__RSC_MANIFEST=(globalThis.__RSC_MANIFEST||{});globalThis.__RSC_MANIFEST["/page"]=';
  if (!text.startsWith(prefix)) throw new Error('Unexpected client manifest format');
  return JSON.parse(text.slice(prefix.length).trim().replace(/;$/, ''));
}

const privateCapabilities = /run write credential|start an offline run|comparison write credential|choose runs to compare|idempotency-key|\/v1\/|x-project-id|\bcpk_|read-only access token|use local live data|api\.(?:openai|anthropic)\.com|generativelanguage\.googleapis\.com/i;
const secret = /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}|\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}|\bAKIA[0-9A-Z]{16}|\b(?:postgres(?:ql)?|mysql):\/\/[A-Za-z0-9]/i;
const request = /\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|\bEventSource\b|\bsendBeacon\b|\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|document\.cookie|["'`]form["'`]/;

export function assertPublicText(path, text, applicationChunk = false) {
  if (secret.test(text) || privateCapabilities.test(text)) throw new Error(`Private capability or credential marker in ${path}`);
  if (/(?:linear|radial|conic)-gradient\s*\(|<(?:linearGradient|radialGradient)\b/i.test(text)) throw new Error(`Gradient in ${path}`);
  if (/chatgpt\.site/i.test(text)) throw new Error(`Old hosting origin in ${path}`);
  if (applicationChunk && request.test(text)) throw new Error(`Request or persistence capability in ${path}`);
}

export function assertPublicPath(path) {
  if (!/^(?:index\.html|404\.html|_not-found\.html|favicon\.svg|og\.png|vercel\.json|(?:index|_not-found|__next\.[\w.-]+)\.txt|_not-found\/__next\.[\w.-]+\.txt|_next\/static\/[A-Za-z0-9_./-]+\.(?:js|css|woff2?|ttf))$/.test(path)) {
    throw new Error(`Unexpected public file: ${path}`);
  }
}
