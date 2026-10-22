FROM python:3.12.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY configs ./configs
RUN uv sync --locked --no-dev
ARG GIT_SHA=unknown
ARG SOURCE_TREE_SHA256=unknown
ENV GIT_SHA=${GIT_SHA} SOURCE_TREE_SHA256=${SOURCE_TREE_SHA256} PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1
USER 10001:10001
