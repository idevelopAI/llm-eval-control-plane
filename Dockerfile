# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv

FROM python:3.14-alpine3.23@sha256:8caa2adfeb414dfe68d8b257f7aea9e205a400521c2b13b2d2e5e731fb8e70e5 AS builder

RUN apk add --no-cache --upgrade \
        'libcrypto3>=3.5.8-r0' \
        'libssl3>=3.5.8-r0' \
        'sqlite-libs>=3.53.4-r0'

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev --no-editable --no-cache

FROM python:3.14-alpine3.23@sha256:8caa2adfeb414dfe68d8b257f7aea9e205a400521c2b13b2d2e5e731fb8e70e5 AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache --upgrade \
        'libcrypto3>=3.5.8-r0' \
        'libssl3>=3.5.8-r0' \
        'sqlite-libs>=3.53.4-r0' \
    && addgroup --system --gid 10001 controlplane \
    && adduser \
        --system \
        --disabled-password \
        --no-create-home \
        --uid 10001 \
        --ingroup controlplane \
        --shell /sbin/nologin \
        controlplane \
    && python -m pip uninstall --yes pip

WORKDIR /app

COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 migrations ./migrations

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "llm_eval_control_plane.api.runtime:create_runtime_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
