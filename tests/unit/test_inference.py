import hashlib
import json
import shutil
from pathlib import Path

import pytest

from inference.bundle import inventory, validate_release
from training.model import load_model

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "artifacts/training"


@pytest.fixture
def release(tmp_path):
    runs = sorted(RUNS.glob("*/model.txt"))
    if not runs:
        pytest.skip("No local M2.1 model artifact available")
    run = runs[-1].parent
    root = tmp_path / "release"
    root.mkdir()
    for source, target in (("model.txt", "model.txt"), ("feature_schema.json", "feature_schema.json"), ("manifest.json", "training_manifest.json")):
        shutil.copy2(run / source, root / target)
    (root / "mlflow_model").mkdir()
    (root / "mlflow_model/MLmodel").write_text("flavors: {}\n")
    manifest = json.loads((run / "manifest.json").read_text())
    files = inventory(root)
    checksum = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    metadata = {
        "format_version": 1,
        "approval_status": "approved",
        "model_name": "nyc-taxi-trip-duration",
        "model_version": "1",
        "mlflow_run_id": "test-mlflow-run",
        "source_run_id": manifest["run_id"],
        "git_sha": manifest.get("git_sha") or "unknown",
        "artifact_checksum": checksum,
        "files": files,
    }
    (root / "release.json").write_text(json.dumps(metadata))
    return root


def test_api_prediction_timezone_parity_and_metrics(release):
    from fastapi.testclient import TestClient
    from inference.app import make_app

    with TestClient(make_app(release)) as client:
        payload = {"pickup_location_id": 1, "dropoff_location_id": 2, "pickup_datetime": "2025-04-07T16:00:00Z"}
        response = client.post("/predict", json=payload)
        assert response.status_code == 200
        result = response.json()
        expected = load_model(release).predict({"tpep_pickup_datetime": "2025-04-07T12:00:00", "PULocationID": 1, "DOLocationID": 2})
        assert result == {"predicted_duration_minutes": expected, "model_name": "nyc-taxi-trip-duration", "model_version": "1"}
        payload["pickup_datetime"] = "2025-04-07T12:00:00-04:00"
        assert client.post("/predict", json=payload).json() == result
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/model-info").status_code == 200
        assert client.get("/docs").status_code == 200
        metrics = client.get("/metrics").text
        assert 'inference_http_requests_total{method="POST",path="/predict",status="200"} 2' in metrics
        assert 'inference_model_info{model_name="nyc-taxi-trip-duration",model_version="1"' in metrics
        assert "pickup_datetime" not in metrics


@pytest.mark.parametrize("change", [
    {"pickup_datetime": "2025-04-07T12:00:00"},
    {"pickup_datetime": "NaN"},
    {"pickup_datetime": None},
    {"pickup_location_id": 0},
    {"dropoff_location_id": 264},
    {"pickup_location_id": 1.5},
    {"pickup_location_id": "Infinity"},
    {"dropoff_location_id": None},
    {"unexpected": "value"},
])
def test_invalid_input_returns_422(release, change):
    from fastapi.testclient import TestClient
    from inference.app import make_app

    with TestClient(make_app(release)) as client:
        payload = {"pickup_location_id": 1, "dropoff_location_id": 2, "pickup_datetime": "2025-04-07T12:00:00-04:00"}
        payload.update(change)
        assert client.post("/predict", json=payload).status_code == 422


def test_missing_model_bad_schema_and_metadata_fail_closed(release):
    validate_release(release)
    (release / "model.txt").unlink()
    with pytest.raises(ValueError, match="checksum"):
        validate_release(release)
    # Restore the model and prove the feature contract and approval are checked.
    source = sorted(RUNS.glob("*/model.txt"))[-1]
    shutil.copy2(source, release / "model.txt")
    schema = json.loads((release / "feature_schema.json").read_text())
    schema["feature_order"] = list(reversed(schema["feature_order"]))
    (release / "feature_schema.json").write_text(json.dumps(schema))
    metadata = json.loads((release / "release.json").read_text())
    metadata["files"] = inventory(release)
    metadata["artifact_checksum"] = hashlib.sha256(json.dumps(metadata["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (release / "release.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="schema"):
        validate_release(release)
    metadata["approval_status"] = "rejected"
    (release / "release.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="unapproved"):
        validate_release(release)


def test_no_release_readiness_503(tmp_path):
    from fastapi.testclient import TestClient
    from inference.app import make_app

    with TestClient(make_app(tmp_path / "missing")) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.get("/model-info").status_code == 503
        payload = {"pickup_location_id": 1, "dropoff_location_id": 2, "pickup_datetime": "2025-04-07T12:00:00-04:00"}
        assert client.post("/predict", json=payload).status_code == 503
