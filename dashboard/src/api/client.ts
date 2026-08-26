import createClient from 'openapi-fetch';

import type { components, operations, paths } from './generated/schema';

export type ReleaseDecision =
  components['schemas']['ReleaseDecisionResponse'];
export type ReleaseDecisionPage =
  components['schemas']['ReleaseDecisionPage'];
export type ReleaseStatus = components['schemas']['ReleaseStatus'];

type ApiErrorDocument = components['schemas']['ApiErrorDocument'];
type ErrorDetail = components['schemas']['ErrorDetail'];
type ReleaseDecisionQuery = NonNullable<
  operations['list_release_decisions']['parameters']['query']
>;

export type RuntimeCredential = Readonly<{
  accessToken: string;
  projectId: string;
}>;

export type CredentialSource = () => RuntimeCredential | null;

export type ApiResult<T> = Readonly<{
  data: T;
  requestId: string | null;
}>;

export class ControlPlaneApiError extends Error {
  readonly code: string;
  readonly details: readonly ErrorDetail[];
  readonly requestId: string | null;
  readonly status: number;

  constructor({
    code,
    details = [],
    message,
    requestId = null,
    status,
  }: {
    code: string;
    details?: readonly ErrorDetail[];
    message: string;
    requestId?: string | null;
    status: number;
  }) {
    super(message);
    this.name = 'ControlPlaneApiError';
    this.code = code;
    this.details = details;
    this.requestId = requestId;
    this.status = status;
  }
}

type OpenApiResponse<T> = Readonly<{
  data?: T;
  error?: unknown;
  response: Response;
}>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isApiErrorDocument(value: unknown): value is ApiErrorDocument {
  if (!isRecord(value) || value.schema_version !== 'api-error/v1') {
    return false;
  }

  const error = value.error;
  return (
    isRecord(error) &&
    typeof error.code === 'string' &&
    typeof error.message === 'string' &&
    typeof error.request_id === 'string' &&
    Array.isArray(error.details)
  );
}

function normalizeFailure(response: Response, payload: unknown) {
  const responseRequestId = response.headers.get('x-request-id');

  if (isApiErrorDocument(payload)) {
    return new ControlPlaneApiError({
      code: payload.error.code,
      details: payload.error.details,
      message: payload.error.message,
      requestId: responseRequestId ?? payload.error.request_id,
      status: response.status,
    });
  }

  return new ControlPlaneApiError({
    code: 'unexpected_response',
    message: 'The control plane returned an unexpected response.',
    requestId: responseRequestId,
    status: response.status,
  });
}

function unwrap<T>({ data, error, response }: OpenApiResponse<T>): ApiResult<T> {
  if (data === undefined) {
    throw normalizeFailure(response, error);
  }

  return {
    data,
    requestId: response.headers.get('x-request-id'),
  };
}

/**
 * Read-only browser client. Credentials must be supplied from volatile memory;
 * this module never persists, logs, caches, or includes them in a URL.
 */
export function createControlPlaneClient(getCredential: CredentialSource) {
  const sameOriginBaseUrl =
    typeof globalThis.location === 'undefined'
      ? ''
      : globalThis.location.origin;
  const client = createClient<paths>({
    baseUrl: sameOriginBaseUrl,
    cache: 'no-store',
    credentials: 'same-origin',
  });

  function credential(): RuntimeCredential {
    const value = getCredential();
    if (!value?.accessToken || !value.projectId) {
      throw new ControlPlaneApiError({
        code: 'authentication_required',
        message: 'A read-only control-plane session is required.',
        status: 401,
      });
    }
    return value;
  }

  function requestHeaders(value: RuntimeCredential) {
    return {
      Authorization: `Bearer ${value.accessToken}`,
    };
  }

  async function request<T>(
    operation: () => Promise<OpenApiResponse<T>>,
  ): Promise<ApiResult<T>> {
    try {
      return unwrap(await operation());
    } catch (error) {
      if (error instanceof ControlPlaneApiError) {
        throw error;
      }
      throw new ControlPlaneApiError({
        code: 'network_error',
        message: 'The control plane could not be reached.',
        status: 0,
      });
    }
  }

  return {
    async getReleaseDecision(
      decisionId: string,
      signal?: AbortSignal,
    ): Promise<ApiResult<ReleaseDecision>> {
      const auth = credential();
      return request(() =>
        client.GET('/v1/release-decisions/{decision_id}', {
          headers: requestHeaders(auth),
          params: {
            header: { 'X-Project-ID': auth.projectId },
            path: { decision_id: decisionId },
          },
          signal,
        }),
      );
    },

    async listReleaseDecisions(
      query: ReleaseDecisionQuery = {},
      signal?: AbortSignal,
    ): Promise<ApiResult<ReleaseDecisionPage>> {
      const auth = credential();
      return request(() =>
        client.GET('/v1/release-decisions', {
          headers: requestHeaders(auth),
          params: {
            header: { 'X-Project-ID': auth.projectId },
            query,
          },
          signal,
        }),
      );
    },
  };
}

export type ControlPlaneClient = ReturnType<typeof createControlPlaneClient>;
