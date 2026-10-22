import json
from datetime import datetime, timedelta

import mlflow.lightgbm
import numpy as np
import pandas as pd
from mlflow import MlflowClient

from training.config import load_config
from training.features import preprocess_request
from training.model import load_model, train
from training.tracking import TrackingConfig, import_run, preview_gate


def _training_run(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for period in ("2025-01", "2025-02", "2025-03", "2025-04"):
        pickup = [datetime.fromisoformat(f"{period}-05T00:00:00") + timedelta(hours=i) for i in range(200)]
        pu = [1 + i % 5 for i in range(200)]
        duration = [5 + start.hour * 2 for start in pickup]
        pd.DataFrame(
            {
                "tpep_pickup_datetime": pickup,
                "tpep_dropoff_datetime": [start + timedelta(minutes=minutes) for start, minutes in zip(pickup, duration)],
                "PULocationID": pu,
                "DOLocationID": [10 + i % 5 for i in range(200)],
            }
        ).to_parquet(data / f"yellow_tripdata_{period}.parquet", index=False)
    config_file = tmp_path / "training.yaml"
    config_file.write_text(
        """seed: 42
periods:
  train: [2025-01, 2025-02]
  validation: [2025-03]
  test: [2025-04]
data_dir: data
output_dir: runs
source_base_url: https://d37ci6vzurychx.cloudfront.net/trip-data
sample_limit_per_month: 160
parquet_batch_size: 17
duration_min_minutes: 1
duration_max_minutes: 180
early_stopping_rounds: 5
model:
  objective: regression_l1
  n_estimators: 60
  learning_rate: 0.1
  num_leaves: 7
  min_child_samples: 3
  n_jobs: 1
  deterministic: true
  force_col_wise: true
  verbosity: -1
""",
        encoding="utf-8",
    )
    return train(load_config(config_file))


def test_tracking_gate_registration_reload_and_idempotent_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # SQLite MLflow's local artifacts stay inside the test directory.
    run_dir = _training_run(tmp_path)
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    config = TrackingConfig("taxi-smoke", "taxi-smoke-model", 0.01)
    assert preview_gate(run_dir, tracking_uri, config)["passed"]

    imported = import_run(run_dir, tracking_uri, config)
    assert imported["validation_status"] == "approved"
    assert imported["model_version"] == "1"
    repeated = import_run(run_dir, tracking_uri, config)
    assert repeated["already_registered"] is True
    assert repeated["model_version"] == "1"
    assert preview_gate(run_dir, tracking_uri, config)["already_registered"] is True
    assert preview_gate(run_dir, tracking_uri, config)["passed"] is True

    client = MlflowClient(tracking_uri=tracking_uri)
    versions = client.search_model_versions("name = 'taxi-smoke-model'")
    assert len(versions) == 1
    assert "champion" not in versions[0].aliases
    assert versions[0].tags["dataset_source_verified"] == "false"
    assert versions[0].tags["validation_status"] == "approved"
    assert client.get_run(imported["mlflow_run_id"]).data.metrics["validation.model.mae_minutes"] >= 0
    artifacts = {item.path for item in client.list_artifacts(imported["mlflow_run_id"], "training")}
    assert "training/manifest.json" in artifacts
    assert "training/feature_schema.json" in artifacts

    request = {"tpep_pickup_datetime": "2025-04-06T08:00:00", "PULocationID": 3, "DOLocationID": 12}
    features = preprocess_request(request)
    raw_prediction = load_model(run_dir).predict(request)
    registered = mlflow.lightgbm.load_model("models:/taxi-smoke-model/1")
    assert np.isclose(registered.predict(features)[0], raw_prediction)
    assert json.loads((run_dir / "manifest.json").read_text())["datasets"]["2025-03"]["source_verified"] is False


def test_rejected_candidate_is_still_tracked_without_model_version(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_dir = _training_run(tmp_path)
    tracking_uri = f"sqlite:///{tmp_path / 'rejected-mlflow.db'}"
    config = TrackingConfig("taxi-rejected", "taxi-rejected-model", 0.999999)
    assert not preview_gate(run_dir, tracking_uri, config)["passed"]

    result = import_run(run_dir, tracking_uri, config)
    assert result["validation_status"] == "rejected"
    assert result["model_version"] is None
    client = MlflowClient(tracking_uri=tracking_uri)
    assert client.get_run(result["mlflow_run_id"]).data.tags["validation_status"] == "rejected"
    assert list(client.search_model_versions("name = 'taxi-rejected-model'")) == []
