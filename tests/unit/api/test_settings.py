from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

from pytest import raises

from llm_eval_control_plane.api.settings import (
    RuntimeConfigurationError,
    WorkerSettings,
    database_url_from_environment,
    max_body_bytes_from_environment,
    worker_settings_from_environment,
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


def test_worker_settings_defaults_are_bounded_and_frozen() -> None:
    settings = worker_settings_from_environment({})

    assert settings == WorkerSettings()
    assert settings == WorkerSettings(
        max_attempts=3,
        lease_seconds=30,
        heartbeat_seconds=10,
        poll_milliseconds=500,
        reaper_batch=50,
        backoff_base_seconds=1,
        backoff_max_seconds=60,
        health_file=Path("/tmp/control-plane-worker.ready"),
    )
    with raises(FrozenInstanceError):
        settings.max_attempts = 4  # type: ignore[misc]


def test_worker_settings_accept_inclusive_bounds_and_relationships() -> None:
    minimum = worker_settings_from_environment(
        {
            "CONTROL_PLANE_WORKER_MAX_ATTEMPTS": "1",
            "CONTROL_PLANE_WORKER_LEASE_SECONDS": "5",
            "CONTROL_PLANE_WORKER_HEARTBEAT_SECONDS": "1",
            "CONTROL_PLANE_WORKER_POLL_MILLISECONDS": "50",
            "CONTROL_PLANE_WORKER_REAPER_BATCH": "1",
            "CONTROL_PLANE_WORKER_BACKOFF_BASE_SECONDS": "1",
            "CONTROL_PLANE_WORKER_BACKOFF_MAX_SECONDS": "1",
            "CONTROL_PLANE_WORKER_HEALTH_FILE": "/tmp/minimum-worker.ready",
        }
    )
    maximum = worker_settings_from_environment(
        {
            "CONTROL_PLANE_WORKER_MAX_ATTEMPTS": "10",
            "CONTROL_PLANE_WORKER_LEASE_SECONDS": "3600",
            "CONTROL_PLANE_WORKER_HEARTBEAT_SECONDS": "1800",
            "CONTROL_PLANE_WORKER_POLL_MILLISECONDS": "60000",
            "CONTROL_PLANE_WORKER_REAPER_BATCH": "100",
            "CONTROL_PLANE_WORKER_BACKOFF_BASE_SECONDS": "300",
            "CONTROL_PLANE_WORKER_BACKOFF_MAX_SECONDS": "3600",
            "CONTROL_PLANE_WORKER_HEALTH_FILE": "/tmp/maximum-worker.ready",
        }
    )

    assert minimum.max_attempts == 1
    assert minimum.health_file == Path("/tmp/minimum-worker.ready")
    assert maximum.heartbeat_seconds * 2 == maximum.lease_seconds
    assert maximum.backoff_max_seconds == 3_600


def test_worker_settings_reject_non_ascii_decimal_values() -> None:
    key = "CONTROL_PLANE_WORKER_MAX_ATTEMPTS"
    for value in (
        "",
        "true",
        "false",
        "+1",
        "-1",
        " 1",
        "1 ",
        "1.0",
        "1_0",
        "\u0661",
    ):
        with raises(RuntimeConfigurationError, match="Worker configuration"):
            worker_settings_from_environment({key: value})
    with raises(RuntimeConfigurationError, match="Worker configuration"):
        worker_settings_from_environment(cast(Mapping[str, str], {key: True}))


def test_worker_settings_reject_each_out_of_bounds_value() -> None:
    invalid = (
        ("CONTROL_PLANE_WORKER_MAX_ATTEMPTS", "0"),
        ("CONTROL_PLANE_WORKER_MAX_ATTEMPTS", "11"),
        ("CONTROL_PLANE_WORKER_LEASE_SECONDS", "4"),
        ("CONTROL_PLANE_WORKER_LEASE_SECONDS", "3601"),
        ("CONTROL_PLANE_WORKER_HEARTBEAT_SECONDS", "0"),
        ("CONTROL_PLANE_WORKER_HEARTBEAT_SECONDS", "1801"),
        ("CONTROL_PLANE_WORKER_POLL_MILLISECONDS", "49"),
        ("CONTROL_PLANE_WORKER_POLL_MILLISECONDS", "60001"),
        ("CONTROL_PLANE_WORKER_REAPER_BATCH", "0"),
        ("CONTROL_PLANE_WORKER_REAPER_BATCH", "101"),
        ("CONTROL_PLANE_WORKER_BACKOFF_BASE_SECONDS", "0"),
        ("CONTROL_PLANE_WORKER_BACKOFF_BASE_SECONDS", "301"),
        ("CONTROL_PLANE_WORKER_BACKOFF_MAX_SECONDS", "0"),
        ("CONTROL_PLANE_WORKER_BACKOFF_MAX_SECONDS", "3601"),
    )
    for key, value in invalid:
        with raises(RuntimeConfigurationError, match="Worker configuration"):
            worker_settings_from_environment({key: value})


def test_worker_settings_reject_ambiguous_timing_and_backoff() -> None:
    with raises(RuntimeConfigurationError, match="Worker configuration"):
        worker_settings_from_environment(
            {
                "CONTROL_PLANE_WORKER_LEASE_SECONDS": "20",
                "CONTROL_PLANE_WORKER_HEARTBEAT_SECONDS": "11",
            }
        )
    with raises(RuntimeConfigurationError, match="Worker configuration"):
        worker_settings_from_environment(
            {
                "CONTROL_PLANE_WORKER_BACKOFF_BASE_SECONDS": "61",
                "CONTROL_PLANE_WORKER_BACKOFF_MAX_SECONDS": "60",
            }
        )


def test_worker_health_file_is_absolute_bounded_and_error_safe() -> None:
    sentinel = "private-worker-path"
    invalid_paths = (
        "",
        sentinel,
        "/",
        "/tmp/",
        f"/tmp/../{sentinel}",
        f"/tmp/{sentinel}\nvalue",
        f"/tmp/{sentinel}\x00value",
        f"/{'x' * 4_096}",
    )
    for value in invalid_paths:
        with raises(RuntimeConfigurationError) as captured:
            worker_settings_from_environment(
                {"CONTROL_PLANE_WORKER_HEALTH_FILE": value}
            )
        if value:
            assert value not in str(captured.value)
        assert sentinel not in str(captured.value)


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
