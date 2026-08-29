import 'server-only';

import {
  executeDashboardRead,
  type DashboardReadDependencies,
} from './dashboard-read-executor';
import type { DashboardReadOperation } from './dashboard-read-operation';
import {
  hostedReadErrorResponse,
  hostedReadSuccessResponse,
} from './hosted-read-response';
import type { HostedControlPlaneConfiguration } from './hosted-config';
import { requireHostedReadProvenance } from './request-provenance';

export type DashboardReadOperationResolver = (
  requestUrl: URL,
) => DashboardReadOperation;

export type HostedReadHandlerInput = Readonly<{
  configuration: HostedControlPlaneConfiguration;
  dependencies: DashboardReadDependencies;
  request: Request;
  requestId?: string | null;
  resolveOperation: DashboardReadOperationResolver;
}>;

/**
 * Compose the disabled hosted boundary without reading runtime bindings.
 * A future route must still provide one fixed operation resolver explicitly.
 */
export async function handleHostedDashboardRead({
  configuration,
  dependencies,
  request,
  requestId = null,
  resolveOperation,
}: HostedReadHandlerInput): Promise<Response> {
  try {
    requireHostedReadProvenance(request, configuration);
    const operation = resolveOperation(new URL(request.url));
    const result = await executeDashboardRead(
      operation,
      configuration,
      dependencies,
      request.signal,
    );
    return hostedReadSuccessResponse(result);
  } catch (error) {
    return hostedReadErrorResponse(error, requestId);
  }
}
