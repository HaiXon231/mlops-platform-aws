FROM python:3.12.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --locked --no-dev --no-install-project
ENV PATH="/app/.venv/bin:${PATH}"
COPY deploy/mlflow/server.py /app/server.py
CMD ["python", "/app/server.py"]
