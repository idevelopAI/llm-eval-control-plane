import { HostedBoundaryError } from './hosted-boundary-error';
import {
  requirePlatformOwner,
  type HeaderSource,
} from './platform-identity';

export type HostedConfigurationInput = Readonly<{
  ownerUserId: unknown;
  projectId: unknown;
  readToken: unknown;
  siteOrigin: unknown;
  upstreamOrigin: unknown;
}>;

export type HostedControlPlaneConfiguration = Readonly<{
  authorizeOwner(headers: HeaderSource): void;
  createUpstreamHeaders(): Headers;
  siteOrigin(): string;
  upstreamOrigin(): string;
}>;

const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const TOKEN_PATTERN = /^cpk_[A-Za-z0-9_-]{43}$/;
const IPV4_LITERAL_PATTERN = /^(?:\d{1,3}\.){3}\d{1,3}$/;
const DNS_HOSTNAME_PATTERN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const FORBIDDEN_HOST_SUFFIXES = [
  '.example',
  '.internal',
  '.invalid',
  '.local',
  '.localhost',
  '.test',
];

function configurationError(): HostedBoundaryError {
  return new HostedBoundaryError('service_configuration_invalid');
}

function canonicalIdentifier(value: unknown): string {
  if (typeof value !== 'string' || !IDENTIFIER_PATTERN.test(value)) {
    throw configurationError();
  }
  return value;
}

function canonicalReadToken(value: unknown): string {
  if (typeof value !== 'string' || !TOKEN_PATTERN.test(value)) {
    throw configurationError();
  }
  return value;
}

function canonicalOwnerUserId(value: unknown): string {
  if (typeof value !== 'string') throw configurationError();
  try {
    requirePlatformOwner({ get: () => value }, value);
  } catch {
    throw configurationError();
  }
  return value;
}

function canonicalHttpsOrigin(value: unknown): string {
  if (
    typeof value !== 'string' ||
    value.length > 2048 ||
    value.trim() !== value ||
    !/^[\x21-\x7E]+$/.test(value)
  ) {
    throw configurationError();
  }

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw configurationError();
  }

  const hostname = url.hostname.toLowerCase();
  const hasForbiddenSuffix = FORBIDDEN_HOST_SUFFIXES.some(
    (suffix) => hostname === suffix.slice(1) || hostname.endsWith(suffix),
  );
  if (
    url.protocol !== 'https:' ||
    url.username !== '' ||
    url.password !== '' ||
    url.pathname !== '/' ||
    url.search !== '' ||
    url.hash !== '' ||
    url.port !== '' ||
    IPV4_LITERAL_PATTERN.test(hostname) ||
    !DNS_HOSTNAME_PATTERN.test(hostname) ||
    hasForbiddenSuffix
  ) {
    throw configurationError();
  }
  return url.origin;
}

class Configuration implements HostedControlPlaneConfiguration {
  readonly #ownerUserId: string;
  readonly #projectId: string;
  readonly #readToken: string;
  readonly #siteOrigin: string;
  readonly #upstreamOrigin: string;

  constructor({
    ownerUserId,
    projectId,
    readToken,
    siteOrigin,
    upstreamOrigin,
  }: {
    ownerUserId: string;
    projectId: string;
    readToken: string;
    siteOrigin: string;
    upstreamOrigin: string;
  }) {
    this.#ownerUserId = ownerUserId;
    this.#projectId = projectId;
    this.#readToken = readToken;
    this.#siteOrigin = siteOrigin;
    this.#upstreamOrigin = upstreamOrigin;
    Object.freeze(this);
  }

  authorizeOwner(headers: HeaderSource): void {
    requirePlatformOwner(headers, this.#ownerUserId);
  }

  createUpstreamHeaders(): Headers {
    return new Headers({
      Accept: 'application/json',
      Authorization: `Bearer ${this.#readToken}`,
      'X-Project-ID': this.#projectId,
    });
  }

  siteOrigin(): string {
    return this.#siteOrigin;
  }

  upstreamOrigin(): string {
    return this.#upstreamOrigin;
  }

  toJSON() {
    return { configured: true };
  }

  toString(): string {
    return 'HostedControlPlaneConfiguration()';
  }
}

/** Validate untrusted runtime values without reading a public build variable. */
export function createHostedControlPlaneConfiguration(
  input: HostedConfigurationInput,
): HostedControlPlaneConfiguration {
  const siteOrigin = canonicalHttpsOrigin(input.siteOrigin);
  const upstreamOrigin = canonicalHttpsOrigin(input.upstreamOrigin);
  if (siteOrigin === upstreamOrigin) throw configurationError();

  return new Configuration({
    ownerUserId: canonicalOwnerUserId(input.ownerUserId),
    projectId: canonicalIdentifier(input.projectId),
    readToken: canonicalReadToken(input.readToken),
    siteOrigin,
    upstreamOrigin,
  });
}
