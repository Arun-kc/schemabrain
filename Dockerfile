# syntax=docker/dockerfile:1.7
#
# Two-stage build for a SchemaBrain runtime container.
#
# Stage 1 (builder) installs build deps, builds the package into a venv,
# and pre-pulls the fastembed ONNX model so the runtime image does not
# need to download it on first start.
#
# Stage 2 (runtime) carries the venv and model cache only. No build
# toolchain, no source tree, no apt cache. Runs as a non-root user.
#
# Built and pushed for linux/amd64 and linux/arm64 by the publish
# workflow. Local single-arch builds work via plain `docker build`.

# --- Stage 1: builder ----------------------------------------------------

FROM python:3.11-slim AS builder

# Build deps for psycopg (libpq) and onnxruntime (the fastembed backend).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy the package source. README is included so `pip install .` does not
# error on the project-table `readme = "README.md"` entry.
COPY pyproject.toml README.md ./
COPY schemabrain/ ./schemabrain/

# Build into an isolated venv that the runtime stage will copy whole.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --upgrade pip \
 && pip install .

# Pre-pull the local embedding model into a known path that the runtime
# stage can copy. `FASTEMBED_CACHE_PATH` is the env var the fastembed
# library reads when its `cache_dir` constructor argument is None, which
# is how SchemaBrain instantiates it.
ENV FASTEMBED_CACHE_PATH=/build/.fastembed
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"


# --- Stage 2: runtime ----------------------------------------------------

FROM python:3.11-slim AS runtime

# Runtime libpq + tini for clean signal handling under `docker run`.
# tini reaps zombie children and forwards SIGTERM correctly when the
# container receives a stop signal; without it Python sees SIGKILL.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    libpq5 \
    tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 schemabrain \
 && useradd --system --uid 1000 --gid schemabrain \
    --create-home --home-dir /home/schemabrain \
    --shell /bin/bash schemabrain

# Bring the venv and the model cache across from the builder stage.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=schemabrain:schemabrain \
    /build/.fastembed /opt/fastembed_cache

# Put the venv on PATH and point fastembed at the baked cache.
# PYTHONUNBUFFERED keeps stdout/stderr flowing for `docker logs`.
# PYTHONDONTWRITEBYTECODE keeps the runtime layer slim.
ENV PATH="/opt/venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/opt/fastembed_cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/schemabrain

USER schemabrain
WORKDIR /home/schemabrain

# A mount-friendly default location for the SQLite store. Operators
# typically run with `-v ~/.schemabrain:/data` to persist the store
# across container restarts; `--store-path /data/store.db` then points
# the CLI at the mount.
VOLUME ["/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "schemabrain"]
CMD ["--help"]
