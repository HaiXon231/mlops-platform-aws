"""End-to-end parity against an actual MLflow Registry version in local SQLite."""

from pathlib import Path

import mlflow
import mlflow.lightgbm
import numpy as np
import pytest

from inference.bundle import validate_release
from inference.export import export_model
from training.features import preprocess_request
from training.tracking import TrackingConfig, import_run

ROOT = Path(__file__).resolve().parents[2]


def test_approved_registry_export_and_prediction_parity(tmp_path, monkeypatch):
    runs = sorted((ROOT / "artifacts/training").glob("*/model.txt"))
    if not runs:
        pytest.skip("No local M2.1 model artifacts available")
    monkeypatch.chdir(tmp_path)
    run_dir = runs[-1].parent
    uri = f"sqlite:///{tmp_path / 'registry.db'}"
    registered = import_run(run_dir, uri, TrackingConfig("m31-export-test", "m31-taxi-model", 0.01))
    assert registered["validation_status"] == "approved"
    release = tmp_path / "release"
    metadata = export_model(uri, "m31-taxi-model", registered["model_version"], release)
    assert metadata["model_version"] == registered["model_version"]
    assert metadata["source_run_id"] == run_dir.name
    _, image_model = validate_release(release)
    mlflow.set_tracking_uri(uri)
    registry_model = mlflow.lightgbm.load_model(f"models:/m31-taxi-model/{registered['model_version']}")
    for pickup, pu, do in (("2025-04-07T12:00:00", 1, 2), ("2025-01-15T08:30:00", 42, 91)):
        raw = {"tpep_pickup_datetime": pickup, "PULocationID": pu, "DOLocationID": do}
        assert np.isclose(image_model.predict(raw), registry_model.predict(preprocess_request(raw))[0], rtol=1e-10, atol=1e-10)
    with pytest.raises(FileExistsError):
        export_model(uri, "m31-taxi-model", registered["model_version"], release)


def test_export_rejects_unapproved_version(tmp_path, monkeypatch):
    runs = sorted((ROOT / "artifacts/training").glob("*/model.txt"))
    if not runs:
        pytest.skip("No local M2.1 model artifacts available")
    monkeypatch.chdir(tmp_path)
    uri = f"sqlite:///{tmp_path / 'registry.db'}"
    from mlflow import MlflowClient
    client = MlflowClient(tracking_uri=uri)
    client.create_registered_model("unapproved")
    run = client.create_run(client.create_experiment("unapproved-experiment"))
    client.create_model_version("unapproved", source=(tmp_path / "unused-model").as_uri(), run_id=run.info.run_id)
    with pytest.raises(ValueError, match="not approved"):
        export_model(uri, "unapproved", "1", tmp_path / "release")
