"""Launch MLflow with a safely URL-encoded PostgreSQL password."""

import os
from urllib.parse import quote


user = quote(os.environ["MLFLOW_DB_USER"], safe="")
password = quote(os.environ["MLFLOW_DB_PASSWORD"], safe="")
host = os.environ["MLFLOW_DB_HOST"]
name = quote(os.environ["MLFLOW_DB_NAME"], safe="")
backend = f"postgresql+psycopg2://{user}:{password}@{host}:5432/{name}"
artifact_destination = os.environ.get("MLFLOW_ARTIFACTS_DESTINATION", "/mlartifacts")
os.execvp(
    "mlflow",
    [
        "mlflow", "server",
        "--host", "0.0.0.0",
        "--port", "5000",
        "--workers", "1",
        "--backend-store-uri", backend,
        "--serve-artifacts",
        "--artifacts-destination", artifact_destination,
    ],
)
