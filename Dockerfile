# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

FROM python:3.14-alpine3.23@sha256:218761489de417a6eb0808e264cbdd7043ec6659fe5a61898815e9848536541d AS builder

RUN apk upgrade --no-cache \
    && apk add --no-cache --upgrade \
        'libcrypto3>=3.5.8-r0' \
        'libssl3>=3.5.8-r0' \
        'libuuid>=2.41.6-r1' \
        'sqlite-libs>=3.53.4-r0'

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev --no-editable --no-cache

FROM python:3.14-alpine3.23@sha256:218761489de417a6eb0808e264cbdd7043ec6659fe5a61898815e9848536541d AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk upgrade --no-cache \
    && apk add --no-cache --upgrade \
        'libcrypto3>=3.5.8-r0' \
        'libssl3>=3.5.8-r0' \
        'libuuid>=2.41.6-r1' \
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
