import createClient from 'openapi-fetch';

import type { components, operations, paths } from './generated/schema';
import { isRuntimeCredential } from '../security/runtime-credential-vault';
import type {
  CredentialSource,
  RuntimeCredential,
} from '../security/runtime-credential-vault';
import {
  isReleaseDecision,
  isReleaseDecisionCasePage,
  isReleaseDecisionDistributions,
  isReleaseDecisionPage,
} from './validation';

export type ReleaseDecision =
  components['schemas']['ReleaseDecisionResponse'];
export type ReleaseDecisionPage =
  components['schemas']['ReleaseDecisionPage'];
export type ReleaseDecisionCasePage =
  components['schemas']['ReleaseDecisionCasePage'];
export type ReleaseDecisionDistributions =
  components['schemas']['ReleaseDecisionDistributionsResponse'];
export type ReleaseCaseChange = components['schemas']['CaseChange'];
export type ReleaseStatus = components['schemas']['ReleaseStatus'];

type ApiErrorDocument = components['schemas']['ApiErrorDocument'];
type ReleaseDecisionQuery = NonNullable<
  operations['list_release_decisions']['parameters']['query']
>;
type ReleaseDecisionCaseQuery = NonNullable<
  operations['list_release_decision_cases']['parameters']['query']
>;
type ReleaseDecisionDistributionQuery = NonNullable<
  operations['get_release_decision_distributions']['parameters']['query']
>;

const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export type { CredentialSource, RuntimeCredential };

export type ApiResult<T> = Readonly<{
  data: T;
  requestId: string | null;
}>;

export class ControlPlaneApiError extends Error {
  readonly code: string;
  readonly requestId: string | null;
  readonly status: number;

  constructor({
    code,
    message,
    requestId = null,
    status,
  }: {
    code: string;
    message: string;
    requestId?: string | null;
    status: number;
  }) {
    super(message);
    this.name = 'ControlPlaneApiError';
    this.code = code;
    this.requestId = requestId;
    this.status = status;
  }
}

const SAFE_ERROR_CODES = new Set([
  'authentication_required',
  'idempotency_conflict',
  'internal_error',
  'invalid_cursor',
  'invalid_json',
  'invalid_request',
  'invalid_submission',
  'permission_denied',
  'persistence_unavailable',
  'request_body_too_large',
  'resource_conflict',
  'resource_not_found',
  'unsupported_content_encoding',
  'unsupported_media_type',
]);

function safeErrorMessage(status: number): string {
  if (status === 401) return 'A read-only control-plane session is required.';
  if (status === 403) return 'This session cannot access the selected project.';
  if (status === 404) return 'The requested release evidence was not found.';
  if (status === 409) return 'The release evidence changed before the request completed.';
  if (status === 400 || status === 413 || status === 415 || status === 422) {
    return 'The control plane did not accept this request.';
  }
  return 'The control plane could not complete this request.';
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

function safeRequestId(value: string | null | undefined): string | null {
  return value && REQUEST_ID_PATTERN.test(value) ? value : null;
}

function normalizeFailure(response: Response, payload: unknown) {
  const responseRequestId = safeRequestId(
    response.headers.get('x-request-id'),
  );

  if (isApiErrorDocument(payload)) {
    return new ControlPlaneApiError({
      code: SAFE_ERROR_CODES.has(payload.error.code)
        ? payload.error.code
        : 'unexpected_response',
      message: safeErrorMessage(response.status),
      requestId:
        responseRequestId ?? safeRequestId(payload.error.request_id),
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

function unwrap<T>(
  { data, error, response }: OpenApiResponse<T>,
  validate: (value: unknown) => value is T,
): ApiResult<T> {
  if (data === undefined) {
    throw normalizeFailure(response, error);
  }
  if (!validate(data)) {
    throw new ControlPlaneApiError({
      code: 'unexpected_response',
      message: 'The control plane returned an unexpected response.',
      requestId: safeRequestId(response.headers.get('x-request-id')),
      status: response.status,
    });
  }

  return {
    data,
    requestId: safeRequestId(response.headers.get('x-request-id')),
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
    mode: 'same-origin',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
  });

  function credential(): RuntimeCredential {
    let value: RuntimeCredential | null;
    try {
      value = getCredential();
    } catch {
      throw new ControlPlaneApiError({
        code: 'authentication_required',
        message: 'A read-only control-plane session is required.',
        status: 401,
      });
    }
    if (!isRuntimeCredential(value)) {
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
    validate: (value: unknown) => value is T,
    signal?: AbortSignal,
  ): Promise<ApiResult<T>> {
    try {
      return unwrap(await operation(), validate);
    } catch (error) {
      if (error instanceof ControlPlaneApiError) {
        throw error;
      }
      if (signal?.aborted || (isRecord(error) && error.name === 'AbortError')) {
        throw new DOMException('The request was canceled.', 'AbortError');
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
      return request(
        () =>
          client.GET('/v1/release-decisions/{decision_id}', {
            headers: requestHeaders(auth),
            params: {
              header: { 'X-Project-ID': auth.projectId },
              path: { decision_id: decisionId },
            },
            signal,
          }),
        isReleaseDecision,
        signal,
      );
    },

    async listReleaseDecisions(
      query: ReleaseDecisionQuery = {},
      signal?: AbortSignal,
    ): Promise<ApiResult<ReleaseDecisionPage>> {
      const auth = credential();
      return request(
        () =>
          client.GET('/v1/release-decisions', {
            headers: requestHeaders(auth),
            params: {
              header: { 'X-Project-ID': auth.projectId },
              query,
            },
            signal,
          }),
        isReleaseDecisionPage,
        signal,
      );
    },

    async listReleaseDecisionCases(
      decisionId: string,
      query: ReleaseDecisionCaseQuery,
      signal?: AbortSignal,
    ): Promise<ApiResult<ReleaseDecisionCasePage>> {
      const auth = credential();
      return request(
        () =>
          client.GET('/v1/release-decisions/{decision_id}/cases', {
            headers: requestHeaders(auth),
            params: {
              header: { 'X-Project-ID': auth.projectId },
              path: { decision_id: decisionId },
              query,
            },
            signal,
          }),
        isReleaseDecisionCasePage,
        signal,
      );
    },

    async getReleaseDecisionDistributions(
      decisionId: string,
      query: ReleaseDecisionDistributionQuery,
      signal?: AbortSignal,
    ): Promise<ApiResult<ReleaseDecisionDistributions>> {
      const auth = credential();
      return request(
        () =>
          client.GET('/v1/release-decisions/{decision_id}/distributions', {
            headers: requestHeaders(auth),
            params: {
              header: { 'X-Project-ID': auth.projectId },
              path: { decision_id: decisionId },
              query,
            },
            signal,
          }),
        isReleaseDecisionDistributions,
        signal,
      );
    },
  };
}

export type ControlPlaneClient = ReturnType<typeof createControlPlaneClient>;
