# syntax=docker/dockerfile:1

# --- Frontend build stage ---
# Always native: the output is static files, identical on every architecture,
# and npm ci under QEMU can crash and then hang the release build.
FROM --platform=$BUILDPLATFORM node:26-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Runtime stage ---
FROM python:3.11-slim AS runtime
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GHOSTSTACK_DB_PATH=/app/data/ghoststack.db \
    GHOSTSTACK_HOST=0.0.0.0 \
    GHOSTSTACK_PORT=8000

COPY requirements.txt ./
# The base image's pip vendors a stale CA bundle that still trusts distrusted
# roots. Install against the OS store instead, then drop the packaging tools:
# nothing needs them at runtime, and they only add CVEs. Use --cert, not
# --use-feature=truststore: pip 24.0 still honours its vendored bundle with
# truststore enabled.
RUN pip install --no-cache-dir --cert /etc/ssl/certs/ca-certificates.crt -r requirements.txt \
    && pip uninstall -y setuptools wheel pip

COPY backend/ ./backend/
COPY run.py alembic.ini ./
COPY --from=frontend /build/dist ./frontend/dist

RUN useradd -u 1000 -m -s /usr/sbin/nologin ghoststack \
    && mkdir -p /app/data \
    && chown -R ghoststack:ghoststack /app

USER ghoststack

VOLUME ["/app/data"]
EXPOSE 8000

CMD ["python", "run.py"]
