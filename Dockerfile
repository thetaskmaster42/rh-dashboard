# rh-dashboard — no dependencies to install, so no pip layer and no venv.
# The whole image is a Python base plus this repo.
FROM python:3.13-slim

# PYTHONDONTWRITEBYTECODE matters here: the chart runs the container with
# readOnlyRootFilesystem, so a __pycache__ write would fail on every import.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
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
# outside a container.
CMD ["python", "-m", "rh_dashboard.cli", "serve", \
     "--host", "0.0.0.0", "--port", "8080", \
     "-i", "/data/input", "-o", "/data/output"]
