# Base image pinned by its multi-arch index digest (python:3.12.15-slim-bookworm, published 2026-10-06).
# Bump deliberately: docker buildx imagetools inspect python:3.12-slim-bookworm
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
# Wheels only, every file checked against the hashes in requirements.lock (exported from uv.lock).
RUN /opt/venv/bin/pip install --no-deps --require-hashes --only-binary=:all: -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 icey \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin icey \
 && install -d -o 10001 -g 10001 -m 0750 /data /data/backups
COPY --from=build /opt/venv /opt/venv
COPY src/icey_stats /app/icey_stats
RUN python -m compileall -q /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    ICEY_STATS_DB_PATH=/data/icey-stats.duckdb
ARG RELEASE_COMMIT=development
LABEL org.opencontainers.image.title=icey-stats \
      org.opencontainers.image.source=https://github.com/leonardovida/icey-stats \
      org.opencontainers.image.revision=$RELEASE_COMMIT
WORKDIR /app
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "icey_stats.admin", "healthcheck"]
STOPSIGNAL SIGTERM
CMD ["python", "-m", "icey_stats"]
