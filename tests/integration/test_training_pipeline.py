import json
import subprocess
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from training.config import load_config
from training.data import download_month, sample_month
from training.model import load_model, train


def _write_month(directory, period, count=80):
    start = datetime.fromisoformat(f"{period}-05T00:00:00")
    pickup = [start + timedelta(hours=i) for i in range(count)]
    durations = [5 + (i % 20) * 2 for i in range(count)]
    frame = pd.DataFrame(
        {
            "tpep_pickup_datetime": pickup,
            "tpep_dropoff_datetime": [p + timedelta(minutes=d) for p, d in zip(pickup, durations)],
            "PULocationID": [1 + i % 5 for i in range(count)],
            "DOLocationID": [10 + i % 5 for i in range(count)],
            "trip_distance": [999.0] * count,
            "fare_amount": [999.0] * count,
        }
    )
    frame.to_parquet(directory / f"yellow_tripdata_{period}.parquet", index=False)


def _config(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for period in ("2025-01", "2025-02", "2025-03", "2025-04"):
        _write_month(data, period)
    config_path = tmp_path / "training.yaml"
    config_path.write_text(
        """seed: 17
periods:
  train: [2025-01, 2025-02]
  validation: [2025-03]
  test: [2025-04]
data_dir: data
output_dir: runs
source_base_url: https://d37ci6vzurychx.cloudfront.net/trip-data
sample_limit_per_month: 30
parquet_batch_size: 13
duration_min_minutes: 1
duration_max_minutes: 180
early_stopping_rounds: 5
model:
  objective: regression_l1
  n_estimators: 40
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
    return load_config(config_path)


def test_sampling_is_seeded_and_not_first_rows(tmp_path):
    config = _config(tmp_path)
    first, counts = sample_month(config, "2025-01")
    second, _ = sample_month(config, "2025-01")
    assert first.equals(second)
    assert counts == {
        "rows_before_cleaning": 80,
        "rows_after_cleaning": 80,
        "rows_after_sampling": 30,
    }
    assert first["duration_minutes"].tolist() != [5 + (i % 20) * 2 for i in range(30)]


def test_offline_training_artifacts_and_reload(tmp_path):
    config = _config(tmp_path)
    cached = download_month(config, "2025-01")  # Existing synthetic cache: no network.
    assert cached["source_verified"] is False
    assert len(cached["sha256"]) == 64

    run_dir = train(config)
    assert {p.name for p in run_dir.iterdir()} == {
        "model.txt", "metrics.json", "feature_schema.json", "manifest.json"
    }
    metrics = json.loads((run_dir / "metrics.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    schema = json.loads((run_dir / "feature_schema.json").read_text())
    assert manifest["split_rows"] == {"train": 60, "validation": 30, "test": 30}
    assert set(manifest["datasets"]) == {"2025-01", "2025-02", "2025-03", "2025-04"}
    assert all(manifest["datasets"][month]["rows_after_sampling"] == 30 for month in manifest["datasets"])
    assert schema["feature_order"] == [
        "PULocationID", "DOLocationID", "pickup_hour", "pickup_day_of_week", "pickup_month"
    ]
    assert np.isfinite(metrics["validation"]["model"]["mae_minutes"])
    assert np.isfinite(metrics["test"]["baseline_median"]["rmse_minutes"])
    prediction = load_model(run_dir).predict(
        {"PULocationID": 2, "DOLocationID": 11, "tpep_pickup_datetime": "2025-04-05T08:00:00"}
    )
    assert np.isfinite(prediction)
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "training.cli",
            "predict",
            "--model-dir",
            str(run_dir),
            "--request",
            json.dumps({"PULocationID": 2, "DOLocationID": 11, "tpep_pickup_datetime": "2025-04-05T08:00:00"}),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(cli.stdout)["predicted_duration_minutes"] == prediction
