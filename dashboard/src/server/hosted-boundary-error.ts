export type HostedBoundaryErrorCode =
  | 'authentication_required'
  | 'invalid_request'
  | 'method_not_allowed'
  | 'permission_denied'
  | 'request_not_allowed'
  | 'service_configuration_invalid'
  | 'unexpected_upstream_response'
  | 'upstream_unavailable';

type ErrorSpec = Readonly<{
  headers: Readonly<Record<string, string>>;
  message: string;
  status: number;
}>;

const ERROR_SPECS: Readonly<Record<HostedBoundaryErrorCode, ErrorSpec>> =
  Object.freeze({
    authentication_required: {
      headers: {},
      message: 'Authentication is required.',
      status: 401,
    },
    invalid_request: {
      headers: {},
      message: 'The hosted request is invalid.',
      status: 400,
    },
    method_not_allowed: {
      headers: { Allow: 'GET' },
      message: 'The hosted request method is not allowed.',
      status: 405,
    },
    permission_denied: {
      headers: {},
      message: 'Permission is not granted.',
      status: 403,
    },
    request_not_allowed: {
      headers: {},
      message: 'The hosted request is not allowed.',
      status: 403,
    },
    service_configuration_invalid: {
      headers: {},
      message: 'Hosted live evidence is unavailable.',
      status: 503,
    },
    unexpected_upstream_response: {
      headers: {},
      message: 'Hosted live evidence returned an unexpected response.',
      status: 502,
    },
    upstream_unavailable: {
      headers: {},
      message: 'Hosted live evidence is unavailable.',
      status: 503,
    },
  });

/** A fixed, content-free error that is safe to translate at the HTTP boundary. */
export class HostedBoundaryError extends Error {
  readonly code: HostedBoundaryErrorCode;
  readonly responseHeaders: Readonly<Record<string, string>>;
  readonly status: number;

  constructor(code: HostedBoundaryErrorCode) {
    const spec = ERROR_SPECS[code];
    super(spec.message);
    this.name = 'HostedBoundaryError';
    this.code = code;
    this.responseHeaders = Object.freeze({ ...spec.headers });
    this.status = spec.status;
  }

  toJSON() {
    return {
      code: this.code,
      message: this.message,
      status: this.status,
    };
  }
}
