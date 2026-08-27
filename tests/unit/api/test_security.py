from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import cast

from pydantic import ValidationError
from pytest import MonkeyPatch, raises
from starlette.types import Scope

from llm_eval_control_plane.api.security import (
    AuthenticationConfiguration,
    AuthorizationFailure,
    ControlPlaneAuthorizer,
    ControlPlaneScope,
    PrincipalConfiguration,
    SecurityConfigurationError,
    digest_token,
    load_authentication_configuration,
    principal_from_scope,
)

_TOKEN = "cpk_" + ("A" * 43)
_OTHER_TOKEN = "cpk_" + ("B" * 43)
_SENTINEL = "private-authentication-sentinel"


def _configuration(
    *,
    scopes: tuple[ControlPlaneScope, ...] = (
        ControlPlaneScope.CANCEL,
        ControlPlaneScope.READ,
        ControlPlaneScope.WRITE,
        ControlPlaneScope.OBSERVABILITY_READ,
    ),
) -> AuthenticationConfiguration:
    return AuthenticationConfiguration(
        schema_version="control-plane-auth/v1",
        project_id="project-alpha",
        principals=(
            PrincipalConfiguration(
                principal_id="principal-alpha",
                token_digest=digest_token(_TOKEN),
                scopes=tuple(sorted(scopes, key=lambda item: item.value)),
            ),
        ),
    )


def _scope(
    path: str,
    *,
    method: str = "GET",
    token: str = _TOKEN,
    project_id: str = "project-alpha",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "path": path,
            "method": method,
            "headers": headers
            if headers is not None
            else [
                (b"authorization", f"Bearer {token}".encode("ascii")),
                (b"x-project-id", project_id.encode("ascii")),
            ],
            "state": {},
        },
    )


def _failure(result: AuthorizationFailure | None) -> AuthorizationFailure:
    assert isinstance(result, AuthorizationFailure)
    return result


def test_digest_accepts_only_canonical_256_bit_tokens() -> None:
    digest = digest_token(_TOKEN)

    assert digest.startswith("sha256:")
    assert len(digest) == 71
    assert digest == digest_token(_TOKEN)
    assert digest != digest_token(_OTHER_TOKEN)
    for invalid in (
        "",
        "cpk_" + ("A" * 42),
        "cpk_" + ("A" * 44),
        "cpk_" + ("+" * 43),
        "bearer_" + ("A" * 43),
        _SENTINEL,
    ):
        with raises(ValueError) as captured:
            digest_token(invalid)
        assert str(captured.value) == "API credential is invalid"
        if invalid:
            assert invalid not in str(captured.value)


def test_configuration_schema_is_strict_ordered_and_digest_only() -> None:
    configuration = _configuration()

    assert configuration.schema_version == "control-plane-auth/v1"
    assert configuration.project_id == "project-alpha"
    serialized = configuration.model_dump(mode="json")
    assert serialized["principals"][0]["token_digest"] == digest_token(_TOKEN)
    assert _TOKEN not in json.dumps(serialized)
    assert repr(configuration) == "AuthenticationConfiguration()"
    assert repr(configuration.principals[0]) == "PrincipalConfiguration()"

    principal_document: dict[str, object] = {
        "principal_id": "principal-alpha",
        "token_digest": digest_token(_TOKEN),
        "scopes": ["control-plane:read"],
    }
    base: dict[str, object] = {
        "schema_version": "control-plane-auth/v1",
        "project_id": "project-alpha",
        "principals": [principal_document],
    }
    invalid_documents = (
        {**base, "schema_version": "control-plane-auth/v2"},
        {**base, "project_id": "private project"},
        {**base, "unknown": _SENTINEL},
        {**base, "principals": []},
        {
            **base,
            "principals": [
                {**principal_document, "token_digest": _TOKEN},
            ],
        },
        {
            **base,
            "principals": [
                {
                    **principal_document,
                    "scopes": ["control-plane:write", "control-plane:read"],
                }
            ],
        },
    )
    for document in invalid_documents:
        with raises(ValidationError):
            AuthenticationConfiguration.model_validate(document, strict=True)


def test_configuration_rejects_duplicate_principal_ids_and_digests() -> None:
    first = PrincipalConfiguration(
        principal_id="principal-a",
        token_digest=digest_token(_TOKEN),
        scopes=(ControlPlaneScope.READ,),
    )
    duplicate_id = PrincipalConfiguration(
        principal_id="principal-a",
        token_digest=digest_token(_OTHER_TOKEN),
        scopes=(ControlPlaneScope.READ,),
    )
    duplicate_digest = PrincipalConfiguration(
        principal_id="principal-b",
        token_digest=digest_token(_TOKEN),
        scopes=(ControlPlaneScope.READ,),
    )
    reversed_principal = PrincipalConfiguration(
        principal_id="principal-0",
        token_digest=digest_token(_OTHER_TOKEN),
        scopes=(ControlPlaneScope.READ,),
    )

    for principals in (
        (first, duplicate_id),
        (first, duplicate_digest),
        (first, reversed_principal),
    ):
        with raises(ValidationError):
            AuthenticationConfiguration(
                schema_version="control-plane-auth/v1",
                project_id="project-alpha",
                principals=principals,
            )


def test_principal_rejects_duplicate_or_unordered_scopes() -> None:
    for scopes in (
        (ControlPlaneScope.READ, ControlPlaneScope.READ),
        (ControlPlaneScope.WRITE, ControlPlaneScope.READ),
    ):
        with raises(ValidationError):
            PrincipalConfiguration(
                principal_id="principal-a",
                token_digest=digest_token(_TOKEN),
                scopes=scopes,
            )


def test_bounded_file_loader_accepts_strict_digest_configuration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authentication.json"
    path.write_text(
        json.dumps(_configuration().model_dump(mode="json")),
        encoding="utf-8",
    )

    loaded = load_authentication_configuration(path.resolve())
    authorizer = ControlPlaneAuthorizer.from_file(path.resolve())

    assert loaded == _configuration()
    assert repr(authorizer) == "ControlPlaneAuthorizer()"
    assert _TOKEN not in repr(authorizer)


def test_file_loader_rejects_relative_symlink_nonregular_and_oversized_files(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(_configuration().model_dump(mode="json")),
        encoding="utf-8",
    )
    symlink = tmp_path / "authentication-link.json"
    symlink.symlink_to(valid)
    directory = tmp_path / "authentication-directory"
    directory.mkdir()
    oversized = tmp_path / "authentication-large.json"
    oversized.write_bytes(b"x" * (256 * 1024 + 1))

    for path in (
        Path("relative.json"),
        symlink.resolve(strict=False).parent / symlink.name,
        directory.resolve(),
        oversized.resolve(),
        (tmp_path / "missing.json").resolve(),
    ):
        with raises(SecurityConfigurationError) as captured:
            load_authentication_configuration(path)
        assert str(captured.value) == "Authentication configuration is invalid"
        assert str(path) not in str(captured.value)


def test_file_loader_rejects_duplicate_keys_invalid_schema_and_secret_text(
    tmp_path: Path,
) -> None:
    documents = (
        b'{"schema_version":"control-plane-auth/v1",'
        b'"schema_version":"control-plane-auth/v1"}',
        b'{"schema_version":"control-plane-auth/v1"}',
        b'{"schema_version":"control-plane-auth/v1","project_id":"\xff"}',
        json.dumps(
            {
                "schema_version": "control-plane-auth/v1",
                "project_id": "project-alpha",
                "principals": [
                    {
                        "principal_id": "principal-alpha",
                        "token": _SENTINEL,
                        "scopes": ["control-plane:read"],
                    }
                ],
            }
        ).encode(),
    )
    for index, document in enumerate(documents):
        path = tmp_path / f"invalid-{index}.json"
        path.write_bytes(document)
        with raises(SecurityConfigurationError) as captured:
            load_authentication_configuration(path.resolve())
        rendered = f"{captured.value!s} {captured.value!r}"
        assert _SENTINEL not in rendered
        assert str(path) not in rendered


def test_public_paths_do_not_require_credentials() -> None:
    authorizer = ControlPlaneAuthorizer(_configuration())

    for path in (
        "/health/live",
        "/health/ready",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/missing",
    ):
        scope = _scope(path, headers=[])
        assert authorizer.authorize(scope) is None
        assert principal_from_scope(scope) is None

    non_path_scope = cast(Scope, {"type": "http", "path": b"/v1", "state": []})
    assert authorizer.authorize(non_path_scope) is None
    assert principal_from_scope(non_path_scope) is None


def test_valid_credentials_authorize_all_explicit_scopes() -> None:
    authorizer = ControlPlaneAuthorizer(_configuration())
    protected = (
        ("/v1/datasets", "GET"),
        ("/v1/datasets", "HEAD"),
        ("/v1/datasets", "OPTIONS"),
        ("/v1/datasets", "POST"),
        ("/v1/jobs/job-1/cancellation", "POST"),
        ("/v1/unknown", "DELETE"),
        ("/metrics", "GET"),
        ("/metrics/internal", "GET"),
    )

    for path, method in protected:
        scope = _scope(path, method=method)
        assert authorizer.authorize(scope) is None
        principal = principal_from_scope(scope)
        assert principal is not None
        assert principal.project_id == "project-alpha"
        assert principal.principal_id == "principal-alpha"
        assert repr(principal) == "AuthorizedPrincipal()"
        assert _TOKEN not in repr(principal)

    with raises(TypeError, match="authentication configuration is required"):
        ControlPlaneAuthorizer(cast(AuthenticationConfiguration, object()))

    invalid_state = _scope("/v1/jobs")
    invalid_state["state"] = []
    failure = _failure(authorizer.authorize(invalid_state))
    assert failure.status == 401


def test_every_protected_resource_has_one_exact_scope() -> None:
    resources = (
        ("/v1/datasets", "POST", ControlPlaneScope.WRITE),
        ("/v1/datasets", "GET", ControlPlaneScope.READ),
        ("/v1/dataset-revisions/1/example", "GET", ControlPlaneScope.READ),
        ("/v1/runs", "POST", ControlPlaneScope.WRITE),
        ("/v1/runs", "GET", ControlPlaneScope.READ),
        ("/v1/runs/run-one", "GET", ControlPlaneScope.READ),
        ("/v1/jobs", "GET", ControlPlaneScope.READ),
        ("/v1/jobs/job-one", "GET", ControlPlaneScope.READ),
        ("/v1/jobs/job-one/attempts", "GET", ControlPlaneScope.READ),
        (
            "/v1/jobs/job-one/cancellation",
            "POST",
            ControlPlaneScope.CANCEL,
        ),
        ("/v1/comparisons", "POST", ControlPlaneScope.WRITE),
        ("/v1/release-decisions", "GET", ControlPlaneScope.READ),
        (
            "/v1/release-decisions/decision-one",
            "GET",
            ControlPlaneScope.READ,
        ),
        (
            "/v1/release-decisions/decision-one/cases",
            "GET",
            ControlPlaneScope.READ,
        ),
        (
            "/v1/release-decisions/decision-one/distributions",
            "GET",
            ControlPlaneScope.READ,
        ),
        ("/metrics", "GET", ControlPlaneScope.OBSERVABILITY_READ),
    )

    for path, method, required_scope in resources:
        for granted_scope in ControlPlaneScope:
            authorizer = ControlPlaneAuthorizer(_configuration(scopes=(granted_scope,)))
            result = authorizer.authorize(_scope(path, method=method))
            if granted_scope is required_scope:
                assert result is None, (path, method, granted_scope)
            else:
                failure = _failure(result)
                assert failure.status == 403, (path, method, granted_scope)
                assert failure.code == "permission_denied"


def test_root_path_prefix_is_removed_before_selecting_the_required_scope() -> None:
    resources = (
        ("/control-plane/v1/jobs", "GET", ControlPlaneScope.READ),
        ("/control-plane/v1/datasets", "POST", ControlPlaneScope.WRITE),
        (
            "/control-plane/v1/jobs/job-one/cancellation",
            "POST",
            ControlPlaneScope.CANCEL,
        ),
        ("/control-plane/metrics", "GET", ControlPlaneScope.OBSERVABILITY_READ),
    )

    for path, method, required_scope in resources:
        for granted_scope in ControlPlaneScope:
            authorizer = ControlPlaneAuthorizer(_configuration(scopes=(granted_scope,)))
            scope = _scope(path, method=method)
            scope["root_path"] = "/control-plane"

            result = authorizer.authorize(scope)

            if granted_scope is required_scope:
                assert result is None, (path, method, granted_scope)
            else:
                failure = _failure(result)
                assert failure.status == 403, (path, method, granted_scope)


def test_invalid_credentials_have_one_safe_401_and_never_retain_state() -> None:
    authorizer = ControlPlaneAuthorizer(_configuration())
    invalid_headers: tuple[list[tuple[bytes, bytes]], ...] = (
        [],
        [(b"authorization", f"Bearer {_OTHER_TOKEN}".encode())],
        [(b"authorization", f"Basic {_TOKEN}".encode())],
        [(b"authorization", f"bearer {_TOKEN}".encode())],
        [(b"authorization", b"Bearer private-authentication-sentinel")],
        [(b"authorization", f"Bearer {_TOKEN}".encode())] * 2,
        [(b"authorization", b"x" * 129)],
        [(b"authorization", b"Bearer \xff")],
    )
    expected: tuple[object, ...] | None = None
    for headers in invalid_headers:
        scope = _scope("/v1/jobs", headers=headers)
        cast(dict[str, object], scope["state"])["control_plane_principal"] = _SENTINEL

        failure = _failure(authorizer.authorize(scope))
        rendered = (
            f"{failure!r} {failure.message} {failure.code} {dict(failure.headers)}"
        )

        assert failure.status == 401
        assert failure.code == "authentication_required"
        assert dict(failure.headers) == {"WWW-Authenticate": "Bearer"}
        assert principal_from_scope(scope) is None
        assert _SENTINEL not in rendered
        assert _TOKEN not in rendered
        signature = (
            failure.status,
            failure.code,
            failure.message,
            tuple(failure.headers.items()),
        )
        expected = signature if expected is None else expected
        assert signature == expected


def test_valid_token_with_missing_wrong_or_invalid_project_is_safe_403() -> None:
    authorizer = ControlPlaneAuthorizer(_configuration())
    authorization = (b"authorization", f"Bearer {_TOKEN}".encode())
    invalid_headers = (
        [authorization],
        [authorization, (b"x-project-id", b"project-other")],
        [
            authorization,
            (b"x-project-id", b"project-alpha"),
            (b"x-project-id", b"project-alpha"),
        ],
        [authorization, (b"x-project-id", b"x" * 129)],
        [authorization, (b"x-project-id", b"private project")],
        [authorization, (b"x-project-id", b"\xff")],
    )

    for headers in invalid_headers:
        scope = _scope("/v1/jobs", headers=headers)
        failure = _failure(authorizer.authorize(scope))

        assert failure.status == 403
        assert failure.code == "permission_denied"
        assert failure.message == "Permission is not granted"
        assert dict(failure.headers) == {}
        assert principal_from_scope(scope) is None


def test_valid_principal_requires_the_path_specific_scope() -> None:
    cases = (
        ((ControlPlaneScope.READ,), "/v1/datasets", "POST"),
        ((ControlPlaneScope.WRITE,), "/v1/datasets", "GET"),
        ((ControlPlaneScope.WRITE,), "/v1/jobs/job-1/cancellation", "POST"),
        ((ControlPlaneScope.READ,), "/metrics", "GET"),
    )
    for scopes, path, method in cases:
        authorizer = ControlPlaneAuthorizer(_configuration(scopes=scopes))
        scope = _scope(path, method=method)

        failure = _failure(authorizer.authorize(scope))

        assert failure.status == 403
        assert failure.code == "permission_denied"
        assert principal_from_scope(scope) is None


def test_header_names_are_case_insensitive_and_exact_project_value_is_required() -> (
    None
):
    authorizer = ControlPlaneAuthorizer(_configuration())
    scope = _scope(
        "/v1/jobs",
        headers=[
            (b"Authorization", f"Bearer {_TOKEN}".encode()),
            (b"X-Project-ID", b"project-alpha"),
            (b"x-ignored-header", b"ignored"),
        ],
    )

    assert authorizer.authorize(scope) is None
    assert principal_from_scope(scope) is not None


def test_digest_comparison_checks_every_principal(monkeypatch: MonkeyPatch) -> None:
    second_token = "cpk_" + ("C" * 43)
    configuration = AuthenticationConfiguration(
        schema_version="control-plane-auth/v1",
        project_id="project-alpha",
        principals=(
            PrincipalConfiguration(
                principal_id="principal-a",
                token_digest=digest_token(_TOKEN),
                scopes=(ControlPlaneScope.READ,),
            ),
            PrincipalConfiguration(
                principal_id="principal-b",
                token_digest=digest_token(second_token),
                scopes=(ControlPlaneScope.READ,),
            ),
        ),
    )
    comparisons: list[tuple[str | bytes, str | bytes]] = []
    original = secrets.compare_digest

    def capture(first: str | bytes, second: str | bytes) -> bool:
        comparisons.append((first, second))
        if isinstance(first, str) and isinstance(second, str):
            return original(first, second)
        if isinstance(first, bytes) and isinstance(second, bytes):
            return original(first, second)
        return False

    monkeypatch.setattr(
        "llm_eval_control_plane.api.security.secrets.compare_digest", capture
    )
    scope = _scope("/v1/jobs")

    assert ControlPlaneAuthorizer(configuration).authorize(scope) is None
    digest_comparisons = [
        item
        for item in comparisons
        if isinstance(item[0], str) and item[0].startswith("sha256:")
    ]
    assert len(digest_comparisons) == 2


def test_invalid_scope_header_structure_fails_closed() -> None:
    authorizer = ControlPlaneAuthorizer(_configuration())
    malformed_scopes = (
        cast(Scope, {"type": "http", "path": "/v1/jobs", "method": "GET"}),
        cast(
            Scope,
            {
                "type": "http",
                "path": "/v1/jobs",
                "method": "GET",
                "headers": "invalid",
            },
        ),
        cast(
            Scope,
            {
                "type": "http",
                "path": "/v1/jobs",
                "method": "GET",
                "headers": [(b"authorization", _SENTINEL)],
            },
        ),
    )

    for scope in malformed_scopes:
        failure = _failure(authorizer.authorize(scope))
        assert failure.status == 401
        assert _SENTINEL not in f"{failure!r} {failure.message}"


def test_configuration_errors_remain_content_free_when_open_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = (tmp_path / _SENTINEL).resolve()

    def fail_open(_path: Path, _flags: int) -> int:
        raise OSError(_SENTINEL)

    monkeypatch.setattr(os, "open", fail_open)

    with raises(SecurityConfigurationError) as captured:
        load_authentication_configuration(path)

    assert _SENTINEL not in f"{captured.value!s} {captured.value!r}"


def test_configuration_requires_no_follow_support(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = (tmp_path / "authentication.json").resolve()
    path.write_text("{}", encoding="utf-8")
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with raises(SecurityConfigurationError):
        load_authentication_configuration(path)
