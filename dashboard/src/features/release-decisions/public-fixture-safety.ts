import {
  PUBLIC_FIXTURE_CASE_ID_PREFIX,
  PUBLIC_FIXTURE_DECISION_ID_PREFIX,
  PUBLIC_FIXTURE_RUN_ID_PREFIX,
  PUBLIC_FIXTURE_SCHEMA_VERSION,
} from './public-fixture-contract';

export {
  PUBLIC_FIXTURE_CASE_ID_PREFIX,
  PUBLIC_FIXTURE_DECISION_ID_PREFIX,
  PUBLIC_FIXTURE_RUN_ID_PREFIX,
  PUBLIC_FIXTURE_SCHEMA_VERSION,
} from './public-fixture-contract';

const PUBLIC_FIXTURE_EXECUTION_MODE =
  'Offline deterministic evaluation' as const;
const PUBLIC_FIXTURE_RUN_EXECUTION_MODE =
  'offline_deterministic_fixture' as const;

/**
 * Exact field names that may contain source material or private operational data.
 *
 * Matching is deliberately exact (after case folding): safe aggregate fields such
 * as `input_units`, `output_units`, and `target_failures` are not rejected merely
 * because they contain a sensitive word.
 */
export const PUBLIC_FIXTURE_DENIED_EXACT_KEYS = Object.freeze([
  // Raw prompts, inputs, outputs, and expected values.
  'prompt',
  'prompts',
  'rawprompt',
  'raw_prompt',
  'rawprompts',
  'raw_prompts',
  'systemprompt',
  'system_prompt',
  'developerprompt',
  'developer_prompt',
  'userprompt',
  'user_prompt',
  'input',
  'inputs',
  'rawinput',
  'raw_input',
  'output',
  'outputs',
  'rawoutput',
  'raw_output',
  'modeloutput',
  'model_output',
  'actualoutput',
  'actual_output',
  'targetoutput',
  'target_output',
  'expectedoutput',
  'expected_output',
  'response',
  'responses',
  'completion',
  'completions',
  // SQL and row-level data.
  'sql',
  'rawsql',
  'raw_sql',
  'sqlquery',
  'sql_query',
  'query',
  'queries',
  'statement',
  'statements',
  'row',
  'rows',
  'record',
  'records',
  // Credentials, authentication material, and session data.
  'credential',
  'credentials',
  'password',
  'passwords',
  'passwd',
  'passphrase',
  'token',
  'tokens',
  'accesstoken',
  'access_token',
  'refreshtoken',
  'refresh_token',
  'apitoken',
  'api_token',
  'apikey',
  'api_key',
  'secret',
  'secrets',
  'clientsecret',
  'client_secret',
  'privatekey',
  'private_key',
  'authorization',
  'cookie',
  'cookies',
  'session',
  'sessionid',
  'session_id',
  // Direct identifiers and common PII fields.
  'pii',
  'email',
  'emails',
  'emailaddress',
  'email_address',
  'phone',
  'phonenumber',
  'phone_number',
  'fullname',
  'full_name',
  'firstname',
  'first_name',
  'lastname',
  'last_name',
  'username',
  'user_id',
  'customer_id',
  'account_id',
  'person_id',
  'address',
  'streetaddress',
  'street_address',
  'postalcode',
  'postal_code',
  'ipaddress',
  'ip_address',
  'dateofbirth',
  'date_of_birth',
  'dob',
  'ssn',
  'taxid',
  'tax_id',
] as const);

const deniedExactKeys = new Set<string>(PUBLIC_FIXTURE_DENIED_EXACT_KEYS);

type PublicFixtureRun = Readonly<{
  execution_mode: string;
  role: string;
  run_id: string;
  simulated: boolean;
}>;

type PublicFixtureDistribution = Readonly<{
  baseline: PublicFixtureRun;
  candidate: PublicFixtureRun;
  decision_id: string;
}>;

export type PublicSyntheticFixture = Readonly<{
  cases: readonly Readonly<{ id: string }>[];
  distributions: readonly PublicFixtureDistribution[];
  gates: readonly Readonly<{
    gate: Readonly<{
      aggregate: Readonly<{
        evaluator: Readonly<{
          kind: string;
          name: string;
          revision: number;
        }>;
      }>;
    }>;
  }>[];
  release: Readonly<{
    decisionId: string;
    executionMode: string;
    simulated: boolean;
  }>;
  schemaVersion: string;
}>;

type UnsafeStringKind = 'credential' | 'email address' | 'URL';

const suspiciousStringPatterns: readonly Readonly<{
  kind: UnsafeStringKind;
  pattern: RegExp;
}>[] = [
  {
    kind: 'email address',
    pattern: /(^|[^a-z0-9.!#$%&'*+/=?^_`{|}~-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:$|[^a-z0-9-])/i,
  },
  {
    kind: 'URL',
    pattern: /(?:\b(?:https?|ftp):\/\/|\bwww\.)\S+/i,
  },
  {
    kind: 'URL',
    pattern: /\b(?:javascript|data|file|mailto):\S+/i,
  },
  {
    kind: 'credential',
    pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  },
  {
    kind: 'credential',
    pattern: /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/,
  },
  {
    kind: 'credential',
    pattern: /\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}\b/,
  },
  {
    kind: 'credential',
    pattern: /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/,
  },
  {
    kind: 'credential',
    pattern: /\bAIza[A-Za-z0-9_-]{20,}\b/,
  },
  {
    kind: 'credential',
    pattern: /\b(?:sk|rk|pk)[_-](?:live[_-])?[A-Za-z0-9_-]{16,}\b/,
  },
  {
    kind: 'credential',
    pattern: /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/,
  },
  {
    kind: 'credential',
    pattern: /\b(?:basic|bearer)\s+[A-Za-z0-9+/_=.-]{8,}\b/i,
  },
  {
    kind: 'credential',
    pattern: /\b(?:api[ _-]?key|access[ _-]?token|client[ _-]?secret|password|passwd|credential)\s*[:=]\s*\S+/i,
  },
];

function unsafe(message: string): never {
  throw new Error(`Unsafe public fixture: ${message}`);
}

function propertyPath(parent: string, key: string): string {
  return /^[A-Za-z_$][\w$]*$/.test(key)
    ? `${parent}.${key}`
    : `${parent}[${JSON.stringify(key)}]`;
}

function isPlainRecord(value: object): value is Record<string, unknown> {
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertSafeString(value: string, path: string): void {
  const match = suspiciousStringPatterns.find(({ pattern }) => pattern.test(value));
  if (match) unsafe(`${match.kind} string at ${path}.`);
}

/**
 * Recursively validates that a value is deterministic JSON and contains no
 * public-fixture data that could expose source material or private operations.
 */
function assertSafeJsonValue(
  value: unknown,
  path: string,
  ancestors: WeakSet<object>,
): void {
  if (value === null || typeof value === 'boolean') return;
  if (typeof value === 'string') {
    assertSafeString(value, path);
    return;
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) unsafe(`non-finite number at ${path}.`);
    return;
  }
  if (typeof value !== 'object') {
    unsafe(`non-JSON value at ${path}.`);
  }
  if (ancestors.has(value)) unsafe(`cyclic reference at ${path}.`);
  if (!Array.isArray(value) && !isPlainRecord(value)) {
    unsafe(`non-plain object at ${path}.`);
  }

  ancestors.add(value);
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      if (!Object.hasOwn(value, index)) {
        unsafe(`sparse array entry at ${path}[${index}].`);
      }
      assertSafeJsonValue(value[index], `${path}[${index}]`, ancestors);
    }
    const ownKeys = Reflect.ownKeys(value).filter(
      (key) => key !== 'length' && !(typeof key === 'string' && /^\d+$/.test(key)),
    );
    if (ownKeys.length > 0) unsafe(`non-index array property at ${path}.`);
  } else {
    for (const key of Reflect.ownKeys(value)) {
      if (typeof key !== 'string') unsafe(`symbol property at ${path}.`);
      const nextPath = propertyPath(path, key);
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor?.enumerable || !('value' in descriptor)) {
        unsafe(`non-data property at ${nextPath}.`);
      }
      if (deniedExactKeys.has(key.toLowerCase())) {
        unsafe(`denied key at ${nextPath}.`);
      }
      if (key === 'simulated' && descriptor.value !== true) {
        unsafe(`simulated flag must be true at ${nextPath}.`);
      }
      if (
        key === 'executionMode' &&
        descriptor.value !== PUBLIC_FIXTURE_EXECUTION_MODE
      ) {
        unsafe(`execution mode must be deterministic at ${nextPath}.`);
      }
      if (
        key === 'execution_mode' &&
        descriptor.value !== PUBLIC_FIXTURE_RUN_EXECUTION_MODE
      ) {
        unsafe(`run execution mode must be deterministic at ${nextPath}.`);
      }
      assertSafeJsonValue(descriptor.value, nextPath, ancestors);
    }
  }
  ancestors.delete(value);
}

function assertRecord(value: unknown, path: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    unsafe(`expected object at ${path}.`);
  }
  return value as Record<string, unknown>;
}

function assertArray(value: unknown, path: string): readonly unknown[] {
  if (!Array.isArray(value)) unsafe(`expected array at ${path}.`);
  return value;
}

function assertPrefixedIdentifier(
  value: unknown,
  prefix: string,
  path: string,
): asserts value is string {
  if (
    typeof value !== 'string' ||
    !value.startsWith(prefix) ||
    value.length === prefix.length
  ) {
    unsafe(`identifier at ${path} must use the ${prefix} namespace.`);
  }
}

/**
 * Enforces the release-specific portion of the public synthetic fixture contract.
 * The generic recursive scan runs first and never includes rejected string values
 * in its error messages.
 */
export function assertPublicFixtureSafe(
  fixture: unknown,
): asserts fixture is PublicSyntheticFixture {
  assertSafeJsonValue(fixture, '$', new WeakSet<object>());

  const root = assertRecord(fixture, '$');
  if (root.schemaVersion !== PUBLIC_FIXTURE_SCHEMA_VERSION) {
    unsafe('schema version is not the pinned public fixture contract.');
  }

  const release = assertRecord(root.release, '$.release');
  assertPrefixedIdentifier(
    release.decisionId,
    PUBLIC_FIXTURE_DECISION_ID_PREFIX,
    '$.release.decisionId',
  );
  if (release.simulated !== true) unsafe('release must be simulated.');
  if (release.executionMode !== PUBLIC_FIXTURE_EXECUTION_MODE) {
    unsafe('release execution mode must be deterministic.');
  }

  const cases = assertArray(root.cases, '$.cases');
  if (cases.length === 0) unsafe('fixture must include synthetic cases.');
  const caseIds = new Set<string>();
  cases.forEach((value, index) => {
    const item = assertRecord(value, `$.cases[${index}]`);
    assertPrefixedIdentifier(
      item.id,
      PUBLIC_FIXTURE_CASE_ID_PREFIX,
      `$.cases[${index}].id`,
    );
    if (caseIds.has(item.id)) unsafe(`duplicate case identifier at $.cases[${index}].id.`);
    caseIds.add(item.id);
  });

  const gates = assertArray(root.gates, '$.gates');
  const distributions = assertArray(root.distributions, '$.distributions');
  if (gates.length === 0 || distributions.length !== gates.length) {
    unsafe('every synthetic gate must have one deterministic distribution.');
  }

  gates.forEach((value, index) => {
    const gate = assertRecord(value, `$.gates[${index}]`);
    const gateEvidence = assertRecord(gate.gate, `$.gates[${index}].gate`);
    const aggregate = assertRecord(
      gateEvidence.aggregate,
      `$.gates[${index}].gate.aggregate`,
    );
    const evaluator = assertRecord(
      aggregate.evaluator,
      `$.gates[${index}].gate.aggregate.evaluator`,
    );
    if (
      evaluator.kind !== 'evaluator' ||
      evaluator.name !== 'builtin-deterministic' ||
      !Number.isInteger(evaluator.revision) ||
      (evaluator.revision as number) < 1
    ) {
      unsafe(`gate evaluator must be pinned and deterministic at $.gates[${index}].`);
    }
  });

  distributions.forEach((value, distributionIndex) => {
    const path = `$.distributions[${distributionIndex}]`;
    const distribution = assertRecord(value, path);
    if (distribution.decision_id !== release.decisionId) {
      unsafe(`distribution decision identifier does not match at ${path}.`);
    }
    for (const expectedRole of ['baseline', 'candidate'] as const) {
      const runPath = `${path}.${expectedRole}`;
      const run = assertRecord(distribution[expectedRole], runPath);
      assertPrefixedIdentifier(
        run.run_id,
        PUBLIC_FIXTURE_RUN_ID_PREFIX,
        `${runPath}.run_id`,
      );
      if (run.role !== expectedRole) unsafe(`run role does not match at ${runPath}.`);
      if (run.simulated !== true) unsafe(`run must be simulated at ${runPath}.`);
      if (run.execution_mode !== PUBLIC_FIXTURE_RUN_EXECUTION_MODE) {
        unsafe(`run execution mode must be deterministic at ${runPath}.`);
      }
    }
    const baseline = assertRecord(distribution.baseline, `${path}.baseline`);
    const candidate = assertRecord(distribution.candidate, `${path}.candidate`);
    if (baseline.run_id === candidate.run_id) {
      unsafe(`baseline and candidate run identifiers must differ at ${path}.`);
    }
  });
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'string' || typeof value === 'number') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  }
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
    .join(',')}}`;
}

/** Returns a byte-stable, recursively key-sorted JSON representation. */
export function serializePublicFixture(fixture: unknown): string {
  assertPublicFixtureSafe(fixture);
  return canonicalJson(fixture);
}
