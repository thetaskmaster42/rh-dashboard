# rh-dashboard — one dependency (DuckDB), installed with uv into a venv.
#
# This image had no pip layer for most of the project's life, because there was
# nothing to install. DuckDB changed that deliberately; see CLAUDE.md. What has
# NOT changed is that `rh_dashboard` itself is never installed — `pyproject.toml`
# sets `package = false`, so uv provides the dependencies and the code is still
# imported off `sys.path` from /app, exactly as it is from a git checkout.
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.13-slim

# PYTHONDONTWRITEBYTECODE matters here: the chart runs the container with
# readOnlyRootFilesystem, so a __pycache__ write would fail on every import.
# UV_* keep uv from reaching the network at run time or rewriting the lockfile.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, in their own layer, so a source edit does not re-resolve
# them. --frozen means the lockfile is obeyed exactly: a build that would need
# to change it fails instead of silently upgrading DuckDB underneath us.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY rh_dashboard/ ./rh_dashboard/
COPY rh-dashboard README.md ./
# Kept so `selftest` can be run against the deployed image itself.
COPY sample_data/ ./sample_data/

# Statement CSVs and generated dashboards live on the mounted volume, never in
# the image. The directories are created so the container also runs sensibly
# with no volume attached.
RUN mkdir -p /data/input /data/output \
 && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin rh \
 && chown -R 1000:1000 /data

USER 1000:1000
EXPOSE 8080

# 0.0.0.0 because the CLI default (127.0.0.1) is deliberately unreachable from
# outside a container. `python` resolves to the venv via PATH above.
CMD ["python", "-m", "rh_dashboard.cli", "serve", \
     "--host", "0.0.0.0", "--port", "8080", \
     "-i", "/data/input", "-o", "/data/output"]
