from pathlib import Path

from pytest import raises

from llm_eval_control_plane.api.settings import (
    RuntimeConfigurationError,
    database_url_from_environment,
    max_body_bytes_from_environment,
)


def test_direct_postgres_url_is_validated_and_masks_its_password() -> None:
    sentinel = "private-database-password"
    url = database_url_from_environment(
        {
            "CONTROL_PLANE_DATABASE_URL": (
                f"postgresql+psycopg://control_plane:{sentinel}"
                "@database:5432/control_plane"
            )
        }
    )

    assert url.drivername == "postgresql+psycopg"
    assert url.password == sentinel
    assert sentinel not in str(url)


def test_component_configuration_reads_a_bounded_absolute_secret_file(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "database-password"
    secret.write_text("p@ssword:/value\n")
    url = database_url_from_environment(
        {
            "CONTROL_PLANE_DATABASE_HOST": "database",
            "CONTROL_PLANE_DATABASE_PORT": "5432",
            "CONTROL_PLANE_DATABASE_NAME": "control_plane",
            "CONTROL_PLANE_DATABASE_USER": "control_plane",
            "CONTROL_PLANE_DATABASE_PASSWORD_FILE": str(secret),
        }
    )

    assert url.password == "p@ssword:/value"
    assert url.host == "database"
    assert url.port == 5432
    assert "p@ssword" not in str(url)


def test_configuration_errors_never_retain_secret_values(tmp_path: Path) -> None:
    sentinel = "private-secret-value"
    secret = tmp_path / "database-password"
    secret.write_text(sentinel)
    common = {
        "CONTROL_PLANE_DATABASE_HOST": "database",
        "CONTROL_PLANE_DATABASE_PORT": "5432",
        "CONTROL_PLANE_DATABASE_NAME": "control_plane",
        "CONTROL_PLANE_DATABASE_USER": "control_plane",
        "CONTROL_PLANE_DATABASE_PASSWORD_FILE": str(secret),
    }

    invalid_configurations = (
        {
            **common,
            "CONTROL_PLANE_DATABASE_URL": (
                f"postgresql+psycopg://user:{sentinel}@database/control_plane"
            ),
        },
        {"CONTROL_PLANE_DATABASE_URL": f"sqlite+pysqlite:///{sentinel}"},
        {**common, "CONTROL_PLANE_DATABASE_PORT": "not-a-port"},
        {**common, "CONTROL_PLANE_DATABASE_PASSWORD_FILE": "relative-secret"},
    )
    for configuration in invalid_configurations:
        with raises(RuntimeConfigurationError) as captured:
            database_url_from_environment(configuration)
        assert sentinel not in str(captured.value)
        assert str(secret) not in str(captured.value)


def test_secret_file_symlinks_and_oversized_values_fail_closed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text("private-password")
    symlink = tmp_path / "secret-link"
    symlink.symlink_to(target)
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * 4_097)

    def configuration(path: Path) -> dict[str, str]:
        return {
            "CONTROL_PLANE_DATABASE_HOST": "database",
            "CONTROL_PLANE_DATABASE_PORT": "5432",
            "CONTROL_PLANE_DATABASE_NAME": "control_plane",
            "CONTROL_PLANE_DATABASE_USER": "control_plane",
            "CONTROL_PLANE_DATABASE_PASSWORD_FILE": str(path),
        }

    for path in (symlink, oversized):
        with raises(RuntimeConfigurationError, match="secret file"):
            database_url_from_environment(configuration(path))


def test_body_limit_has_safe_defaults_and_hard_ceiling() -> None:
    assert max_body_bytes_from_environment({}) == 4 * 1024 * 1024
    assert max_body_bytes_from_environment({"CONTROL_PLANE_MAX_BODY_BYTES": "64"}) == 64

    for value in ("0", "16777217", "not-a-number"):
        with raises(RuntimeConfigurationError, match="body limit"):
            max_body_bytes_from_environment({"CONTROL_PLANE_MAX_BODY_BYTES": value})


def test_url_sources_are_unambiguous_complete_and_driver_bounded() -> None:
    with raises(RuntimeConfigurationError, match="required"):
        database_url_from_environment({})
    with raises(RuntimeConfigurationError, match="incomplete"):
        database_url_from_environment({"CONTROL_PLANE_DATABASE_HOST": "database"})

    sqlite = database_url_from_environment(
        {},
        fallback_url="sqlite+pysqlite:///:memory:",
        allow_sqlite=True,
    )
    assert sqlite.drivername == "sqlite+pysqlite"

    invalid_urls = (
        "",
        "x" * 4_097,
        "://invalid",
        "sqlite+pysqlite:///:memory:",
        "postgresql+psycopg://database",
    )
    for value in invalid_urls:
        with raises(RuntimeConfigurationError):
            database_url_from_environment(
                {"CONTROL_PLANE_DATABASE_URL": value},
            )


def test_component_names_ports_and_secret_contents_fail_closed(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "database-password"
    secret.write_text("safe-password")
    common = {
        "CONTROL_PLANE_DATABASE_HOST": "database",
        "CONTROL_PLANE_DATABASE_PORT": "5432",
        "CONTROL_PLANE_DATABASE_NAME": "control_plane",
        "CONTROL_PLANE_DATABASE_USER": "control_plane",
        "CONTROL_PLANE_DATABASE_PASSWORD_FILE": str(secret),
    }

    invalid_components = (
        {**common, "CONTROL_PLANE_DATABASE_HOST": "bad_host"},
        {**common, "CONTROL_PLANE_DATABASE_NAME": "9invalid"},
        {**common, "CONTROL_PLANE_DATABASE_USER": "private user"},
        {**common, "CONTROL_PLANE_DATABASE_PORT": "0"},
        {**common, "CONTROL_PLANE_DATABASE_PORT": "65536"},
    )
    for configuration in invalid_components:
        with raises(RuntimeConfigurationError, match="configuration is invalid"):
            database_url_from_environment(configuration)

    invalid_secret_payloads = (
        b"",
        b"\xff",
        b"private\x00value",
        b"private\nvalue",
        b"x" * 1_025,
    )
    for index, payload in enumerate(invalid_secret_payloads):
        path = tmp_path / f"invalid-secret-{index}"
        path.write_bytes(payload)
        with raises(RuntimeConfigurationError, match="secret file"):
            database_url_from_environment(
                {**common, "CONTROL_PLANE_DATABASE_PASSWORD_FILE": str(path)}
            )

    for path in (tmp_path, tmp_path / "missing-secret"):
        with raises(RuntimeConfigurationError, match="secret file"):
            database_url_from_environment(
                {**common, "CONTROL_PLANE_DATABASE_PASSWORD_FILE": str(path)}
            )
