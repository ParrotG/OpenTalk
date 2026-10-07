# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.21 AS uv
FROM python:3.11.14-slim-bookworm AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

# The test image never receives provider credentials or persistent demo volumes.
FROM dependencies AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY backend ./backend
COPY config ./config
COPY tests ./tests
ENV PYTHONPATH=/app/backend PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["python", "-m", "pytest", "-q"]

FROM python:3.11.14-slim-bookworm AS runtime
WORKDIR /app
RUN useradd --uid 10001 --create-home app \
    && mkdir -p data/booking data/sessions logs \
    && chown -R app:app /app
COPY --from=dependencies /app/.venv ./.venv
COPY backend ./backend
COPY config ./config
ENV PYTHONPATH=/app/backend PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
CMD ["python", "-m", "opentalk.voice.worker", "start", "--log-level", "info", "--drain-timeout", "15"]
