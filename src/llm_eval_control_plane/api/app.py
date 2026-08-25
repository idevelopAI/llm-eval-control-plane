"""FastAPI application factory for the durable control-plane API."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from llm_eval_control_plane.api.contracts import (
    ApiErrorDocument,
    ComparisonCreateRequest,
    ComparisonSubmissionResponse,
    DatasetCreateRequest,
    DatasetListItemResponse,
    DatasetPage,
    DatasetResponse,
    ErrorDetail,
    HealthResponse,
    JobAttemptListResponse,
    JobAttemptResponse,
    JobCancellationRequest,
    JobPage,
    JobResponse,
    ReleaseDecisionListItemResponse,
    ReleaseDecisionPage,
    ReleaseDecisionResponse,
    RunCreateRequest,
    RunListItemResponse,
    RunPage,
    RunResponse,
    RunSubmissionResponse,
)
from llm_eval_control_plane.api.middleware import (
    ApiBoundaryMiddleware,
    error_document,
    request_id_from_scope,
)
from llm_eval_control_plane.api.observability import (
    ApiObservabilityMiddleware,
    current_traceparent,
    set_error_code,
)
from llm_eval_control_plane.api.security import ControlPlaneAuthorizer
from llm_eval_control_plane.application.control_plane import (
    ComparisonSubmission,
    ControlPlaneService,
    ControlPlaneServiceError,
    ControlPlaneStoreError,
    IdempotencyConflictError,
    InvalidCursorError,
    InvalidSubmissionError,
    ResourceConflictError,
    ResourceNotFoundError,
    RunSubmission,
    SubmissionResult,
)
from llm_eval_control_plane.domain.comparison import ReleaseStatus
from llm_eval_control_plane.domain.control_plane import JobKind, JobStatus
from llm_eval_control_plane.observability import Observability

_DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
_STABLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_SAFE_LOCATIONS = frozenset(
    {
        "adapter",
        "baseline_run_id",
        "body",
        "candidate_run_id",
        "cases",
        "dataset_name",
        "dataset_revision",
        "evaluators",
        "expected",
        "expected_refusal",
        "expected_schema",
        "header",
        "idempotency-key",
        "input",
        "name",
        "numeric_tolerance",
        "path",
        "query",
        "revision",
        "scenario_overrides",
        "schema_version",
        "slices",
        "spec",
        "target_name",
        "target_revision",
    }
)

IdempotencyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=_IDEMPOTENCY_PATTERN,
    ),
]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(min_length=1, max_length=2048)]
NameQuery = Annotated[
    str | None,
    Query(min_length=1, max_length=128, pattern=_NAME_PATTERN),
]
JobKindQuery = Annotated[JobKind | None, Query()]
JobStatusQuery = Annotated[JobStatus | None, Query()]
ReleaseStatusQuery = Annotated[ReleaseStatus | None, Query()]

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorDocument, "description": "Authentication required"},
    403: {"model": ApiErrorDocument, "description": "Permission denied"},
    400: {"model": ApiErrorDocument, "description": "Invalid request"},
    404: {"model": ApiErrorDocument, "description": "Resource not found"},
    409: {"model": ApiErrorDocument, "description": "Immutable conflict"},
    413: {"model": ApiErrorDocument, "description": "Request body too large"},
    415: {"model": ApiErrorDocument, "description": "Unsupported media type"},
    422: {"model": ApiErrorDocument, "description": "Contract validation failed"},
    500: {"model": ApiErrorDocument, "description": "Internal service error"},
    503: {"model": ApiErrorDocument, "description": "Service unavailable"},
}
_JOB_LOCATION_HEADERS: dict[str, dict[str, object]] = {
    "Location": {
        "description": "Canonical path of the durable submission job",
        "schema": {
            "type": "string",
            "pattern": r"^/v1/jobs/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        },
    }
}


def create_app(
    *,
    service: ControlPlaneService,
    authorizer: ControlPlaneAuthorizer,
    telemetry: Observability,
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
) -> FastAPI:
    """Build an API with injected use cases and no implicit schema mutation."""
    app = FastAPI(
        title="LLM Evaluation Control Plane",
        summary="Durable, content-addressed evaluation and release decisions",
        version="1.2.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        swagger_ui_parameters={"displayOperationId": True},
    )
    app.add_middleware(
        ApiBoundaryMiddleware,
        max_body_bytes=max_body_bytes,
        authorizer=authorizer,
    )
    app.add_middleware(ApiObservabilityMiddleware, telemetry=telemetry)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        details: list[dict[str, object]] = []
        for item in error.errors()[:32]:
            raw_location = item.get("loc", ())
            location = tuple(_safe_location(value) for value in raw_location)
            raw_type = item.get("type")
            error_type = (
                raw_type
                if isinstance(raw_type, str) and _SAFE_ERROR_TYPE.fullmatch(raw_type)
                else "validation_error"
            )
            details.append(
                ErrorDetail(location=location, type=error_type).model_dump(mode="json")
            )
        return _error_response(
            request=request,
            status_code=422,
            code="invalid_request",
            message="Request failed contract validation",
            details=details,
        )

    @app.exception_handler(ControlPlaneServiceError)
    async def service_error_handler(
        request: Request,
        error: ControlPlaneServiceError,
    ) -> JSONResponse:
        status_code = 500
        if isinstance(error, ResourceNotFoundError):
            status_code = 404
        elif isinstance(error, (IdempotencyConflictError, ResourceConflictError)):
            status_code = 409
        elif isinstance(error, InvalidCursorError):
            status_code = 400
        elif isinstance(error, InvalidSubmissionError):
            status_code = 422
        return _error_response(
            request=request,
            status_code=status_code,
            code=error.code,
            message=str(error),
        )

    @app.exception_handler(ControlPlaneStoreError)
    async def store_error_handler(
        request: Request,
        _error: ControlPlaneStoreError,
    ) -> JSONResponse:
        return _error_response(
            request=request,
            status_code=503,
            code="persistence_unavailable",
            message="Control-plane persistence is unavailable",
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(
        request: Request,
        error: HTTPException,
    ) -> JSONResponse:
        code, message = {
            404: ("route_not_found", "Route was not found"),
            405: ("method_not_allowed", "HTTP method is not allowed"),
        }.get(
            error.status_code,
            ("http_error", "HTTP request could not be completed"),
        )
        return _error_response(
            request=request,
            status_code=error.status_code,
            code=code,
            message=message,
            headers=_safe_http_exception_headers(error),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request,
        _error: Exception,
    ) -> JSONResponse:
        return _error_response(
            request=request,
            status_code=500,
            code="internal_error",
            message="Request could not be completed",
        )

    @app.get(
        "/health/live",
        response_model=HealthResponse,
        operation_id="get_liveness",
        tags=["health"],
    )
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        operation_id="get_readiness",
        responses={503: {"model": HealthResponse, "description": "Not ready"}},
        tags=["health"],
    )
    async def readiness(request: Request, response: Response) -> HealthResponse:
        if service.ready():
            return HealthResponse(status="ok")
        set_error_code(request.scope, "readiness_unavailable")
        response.status_code = 503
        return HealthResponse(status="unavailable")

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        document = telemetry.render_metrics()
        return Response(content=document.body, media_type=document.content_type)

    @app.post(
        "/v1/datasets",
        response_model=DatasetResponse,
        status_code=201,
        operation_id="create_dataset_revision",
        responses=_ERROR_RESPONSES,
        tags=["datasets"],
    )
    async def create_dataset(body: DatasetCreateRequest) -> DatasetResponse:
        try:
            dataset = body.to_domain()
        except (ValidationError, ValueError) as error:
            raise InvalidSubmissionError("Dataset revision is invalid") from error
        return DatasetResponse.from_record(service.register_dataset(dataset))

    @app.get(
        "/v1/datasets",
        response_model=DatasetPage,
        operation_id="list_dataset_revisions",
        responses=_ERROR_RESPONSES,
        tags=["datasets"],
    )
    async def list_datasets(
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
        name: NameQuery = None,
    ) -> DatasetPage:
        page = service.list_datasets(limit=limit, cursor=cursor, name=name)
        return DatasetPage(
            items=tuple(
                DatasetListItemResponse.from_record(item) for item in page.items
            ),
            next_cursor=page.next_cursor,
        )

    @app.get(
        "/v1/dataset-revisions/{revision}/{name:path}",
        response_model=DatasetResponse,
        operation_id="get_dataset_revision",
        responses=_ERROR_RESPONSES,
        tags=["datasets"],
    )
    async def get_dataset(
        revision: Annotated[int, Path(gt=0)],
        name: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=_NAME_PATTERN),
        ],
    ) -> DatasetResponse:
        return DatasetResponse.from_record(service.get_dataset(name, revision))

    @app.post(
        "/v1/runs",
        response_model=RunSubmissionResponse,
        status_code=202,
        operation_id="submit_evaluation_run",
        responses={
            **_ERROR_RESPONSES,
            200: {
                "model": RunSubmissionResponse,
                "description": "Terminal replay",
                "headers": _JOB_LOCATION_HEADERS,
            },
            202: {
                "model": RunSubmissionResponse,
                "description": "Accepted new or nonterminal job",
                "headers": _JOB_LOCATION_HEADERS,
            },
        },
        tags=["runs"],
        description=(
            "The API validates and durably enqueues immutable worker input. Execution "
            "is performed asynchronously by leased workers."
        ),
    )
    async def submit_run(
        body: RunCreateRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyHeader,
    ) -> RunSubmissionResponse:
        outcome = await service.submit_run(
            RunSubmission(
                idempotency_key=idempotency_key,
                dataset_name=body.dataset_name,
                dataset_revision=body.dataset_revision,
                target_name=body.target_name,
                target_revision=body.target_revision,
                adapter=body.adapter,
                evaluator_names=tuple(item.value for item in body.evaluators),
                scenario_overrides=body.scenario_overrides,
                traceparent=current_traceparent(),
            )
        )
        response.status_code = _submission_status(outcome)
        response.headers["Location"] = f"/v1/jobs/{outcome.job.job_id}"
        run = None
        if outcome.job.status is JobStatus.SUCCEEDED:
            run = RunResponse.from_record(service.get_run(outcome.job.resource_id))
        return RunSubmissionResponse(
            job=JobResponse.from_record(outcome.job),
            run=run,
        )

    @app.get(
        "/v1/jobs",
        response_model=JobPage,
        operation_id="list_jobs",
        responses=_ERROR_RESPONSES,
        tags=["jobs"],
    )
    async def list_jobs(
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
        kind: JobKindQuery = None,
        status: JobStatusQuery = None,
    ) -> JobPage:
        page = service.list_jobs(
            limit=limit,
            cursor=cursor,
            kind=kind,
            status=status,
        )
        return JobPage(
            items=tuple(JobResponse.from_record(item) for item in page.items),
            next_cursor=page.next_cursor,
        )

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobResponse,
        operation_id="get_job",
        responses=_ERROR_RESPONSES,
        tags=["jobs"],
    )
    async def get_job(
        job_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=_STABLE_ID_PATTERN),
        ],
    ) -> JobResponse:
        return JobResponse.from_record(service.get_job(job_id))

    @app.get(
        "/v1/jobs/{job_id}/attempts",
        response_model=JobAttemptListResponse,
        operation_id="list_job_attempts",
        responses=_ERROR_RESPONSES,
        tags=["jobs"],
    )
    async def list_job_attempts(
        job_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=_STABLE_ID_PATTERN),
        ],
    ) -> JobAttemptListResponse:
        return JobAttemptListResponse(
            items=tuple(
                JobAttemptResponse.from_record(record)
                for record in service.list_job_attempts(job_id)
            )
        )

    @app.post(
        "/v1/jobs/{job_id}/cancellation",
        response_model=JobResponse,
        operation_id="request_job_cancellation",
        responses=_ERROR_RESPONSES,
        tags=["jobs"],
    )
    async def request_job_cancellation(
        body: JobCancellationRequest,
        job_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=_STABLE_ID_PATTERN),
        ],
    ) -> JobResponse:
        del body
        return JobResponse.from_record(service.cancel_job(job_id))

    @app.get(
        "/v1/runs",
        response_model=RunPage,
        operation_id="list_evaluation_runs",
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def list_runs(
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
        dataset_name: NameQuery = None,
    ) -> RunPage:
        page = service.list_runs(
            limit=limit,
            cursor=cursor,
            dataset_name=dataset_name,
        )
        return RunPage(
            items=tuple(RunListItemResponse.from_record(item) for item in page.items),
            next_cursor=page.next_cursor,
        )

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunResponse,
        operation_id="get_evaluation_run",
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def get_run(
        run_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=_RUN_ID_PATTERN),
        ],
    ) -> RunResponse:
        return RunResponse.from_record(service.get_run(run_id))

    @app.post(
        "/v1/comparisons",
        response_model=ComparisonSubmissionResponse,
        status_code=202,
        operation_id="submit_release_comparison",
        responses={
            **_ERROR_RESPONSES,
            200: {
                "model": ComparisonSubmissionResponse,
                "description": "Terminal replay",
                "headers": _JOB_LOCATION_HEADERS,
            },
            202: {
                "model": ComparisonSubmissionResponse,
                "description": "Accepted new or nonterminal job",
                "headers": _JOB_LOCATION_HEADERS,
            },
        },
        tags=["release decisions"],
        description=(
            "The API pins stored run evidence and durably enqueues an asynchronous "
            "release comparison."
        ),
    )
    async def submit_comparison(
        body: ComparisonCreateRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyHeader,
    ) -> ComparisonSubmissionResponse:
        outcome = await service.submit_comparison(
            ComparisonSubmission(
                idempotency_key=idempotency_key,
                dataset_name=body.dataset_name,
                dataset_revision=body.dataset_revision,
                baseline_run_id=body.baseline_run_id,
                candidate_run_id=body.candidate_run_id,
                spec=body.spec.to_domain(),
                traceparent=current_traceparent(),
            )
        )
        response.status_code = _submission_status(outcome)
        response.headers["Location"] = f"/v1/jobs/{outcome.job.job_id}"
        decision = None
        if outcome.job.status is JobStatus.SUCCEEDED:
            decision = ReleaseDecisionResponse.from_record(
                service.get_release_decision(outcome.job.resource_id)
            )
        return ComparisonSubmissionResponse(
            job=JobResponse.from_record(outcome.job),
            decision=decision,
        )

    @app.get(
        "/v1/release-decisions",
        response_model=ReleaseDecisionPage,
        operation_id="list_release_decisions",
        responses=_ERROR_RESPONSES,
        tags=["release decisions"],
    )
    async def list_release_decisions(
        limit: LimitQuery = 50,
        cursor: CursorQuery = None,
        status: ReleaseStatusQuery = None,
    ) -> ReleaseDecisionPage:
        page = service.list_release_decisions(
            limit=limit,
            cursor=cursor,
            status=status,
        )
        return ReleaseDecisionPage(
            items=tuple(
                ReleaseDecisionListItemResponse.from_record(item) for item in page.items
            ),
            next_cursor=page.next_cursor,
        )

    @app.get(
        "/v1/release-decisions/{decision_id}",
        response_model=ReleaseDecisionResponse,
        operation_id="get_release_decision",
        responses=_ERROR_RESPONSES,
        tags=["release decisions"],
    )
    async def get_release_decision(
        decision_id: Annotated[
            str,
            Path(min_length=1, max_length=128, pattern=_STABLE_ID_PATTERN),
        ],
    ) -> ReleaseDecisionResponse:
        return ReleaseDecisionResponse.from_record(
            service.get_release_decision(decision_id)
        )

    _install_openapi_security(app)
    return app


def _submission_status(outcome: SubmissionResult) -> int:
    if not outcome.created and outcome.job.status in {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    }:
        return 200
    return 202


def _safe_location(value: object) -> str | int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in _SAFE_LOCATIONS:
        return value.casefold()
    return "item"


def _safe_http_exception_headers(error: HTTPException) -> dict[str, str] | None:
    """Preserve only bounded protocol headers from framework-generated errors."""
    if error.status_code != 405 or error.headers is None:
        return None
    allow = next(
        (value for name, value in error.headers.items() if name.casefold() == "allow"),
        None,
    )
    if allow is None or re.fullmatch(r"[A-Za-z, ]{1,128}", allow) is None:
        return None
    return {"Allow": allow}


def _error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, object]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    set_error_code(request.scope, code)
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content=error_document(
            code=code,
            message=message,
            request_id=request_id_from_scope(request.scope),
            details=details,
        ),
    )


def _install_openapi_security(app: FastAPI) -> None:
    """Document the bearer and project headers on every protected operation."""
    original_openapi = app.openapi

    def secured_openapi() -> dict[str, Any]:
        document = original_openapi()
        components = document.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes["ProjectBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "cpk_<base64url>",
            "description": "Project-bound opaque API credential",
        }
        project_header = {
            "name": "X-Project-ID",
            "in": "header",
            "required": True,
            "description": "Configured single-deployment project boundary",
            "schema": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": _STABLE_ID_PATTERN,
            },
        }
        for path, path_item in document.get("paths", {}).items():
            if not path.startswith("/v1") or not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in {
                    "delete",
                    "get",
                    "head",
                    "options",
                    "patch",
                    "post",
                    "put",
                } or not isinstance(operation, dict):
                    continue
                operation["security"] = [{"ProjectBearer": []}]
                parameters = operation.setdefault("parameters", [])
                if isinstance(parameters, list):
                    parameters.append(project_header)
        return document

    app.openapi = secured_openapi  # type: ignore[method-assign]


__all__ = ["create_app"]
