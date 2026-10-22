FROM python:3.12.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev
ARG RELEASE_DIR=artifacts/releases/nyc-taxi-trip-duration-v1
ARG SERVING_GIT_SHA=unknown
COPY ${RELEASE_DIR}/ /app/release/
RUN /app/.venv/bin/python -c 'from inference.bundle import validate_release; from pathlib import Path; validate_release(Path("/app/release"))'
ENV PATH="/app/.venv/bin:${PATH}" MODEL_BUNDLE_DIR=/app/release SERVING_GIT_SHA=${SERVING_GIT_SHA} PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "inference.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "25"]
