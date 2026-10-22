"""Read an explicitly approved Registry version into an offline, checksummed release."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path

import mlflow
import mlflow.lightgbm
import numpy as np
from mlflow import MlflowClient
from mlflow.models import Model

from inference.bundle import inventory, validate_release
from training.features import FEATURE_NAMES, preprocess_request
from training.gate import METRIC_DEFINITION
from training.model import load_model


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def export_model(tracking_uri: str, name: str, version: str, output: Path) -> dict:
    if not version.isdecimal() or int(version) < 1:
        raise ValueError("Specify a positive numeric Registry model version")
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient(tracking_uri=tracking_uri)
    registered = client.get_model_version(name, version)
    tags = registered.tags or {}
    if registered.status != "READY":
        raise ValueError("Registry model version is not READY")
    if tags.get("validation_status") != "approved":
        raise ValueError("Registry version is not approved by validation gate")
    for key in ("source_run_id", "source_manifest_sha256", "validation_dataset_key", "validation_metric_definition", "validation_mae_minutes", "git_sha"):
        if not tags.get(key):
            raise ValueError(f"Approved version lacks {key}")
    if tags["validation_metric_definition"] != METRIC_DEFINITION or not math.isfinite(float(tags["validation_mae_minutes"])):
        raise ValueError("Approved version has invalid validation metric metadata")
    run_id = registered.run_id
    if not run_id:
        raise ValueError("Registry version has no MLflow run ID")
    run_tags = client.get_run(run_id).data.tags
    for key in ("source_run_id", "source_manifest_sha256", "validation_dataset_key", "git_sha"):
        if run_tags.get(key) != tags[key]:
            raise ValueError(f"Registry and MLflow run disagree on {key}")
    if run_tags.get("validation_status") != "approved":
        raise ValueError("MLflow run is not approved")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Release output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="m31-export-", dir=output.parent) as temporary:
        root = Path(temporary)
        for source, target in (("training/model.txt", "model.txt"), ("training/feature_schema.json", "feature_schema.json"), ("training/manifest.json", "training_manifest.json")):
            artifact = Path(client.download_artifacts(run_id, source, dst_path=str(root / "downloads")))
            shutil.copy2(artifact, root / target)
        model_source = Path(mlflow.artifacts.download_artifacts(artifact_uri=f"models:/{name}/{version}", dst_path=str(root / "downloads")))
        shutil.copytree(model_source, root / "mlflow_model")
        manifest = _json(root / "training_manifest.json")
        schema = _json(root / "feature_schema.json")
        if manifest.get("run_id") != tags["source_run_id"] or (manifest.get("git_sha") or "unknown") != tags["git_sha"]:
            raise ValueError("Training manifest provenance differs from Registry")
        if hashlib.sha256((root / "training_manifest.json").read_bytes()).hexdigest() != tags["source_manifest_sha256"]:
            raise ValueError("Training manifest checksum differs from Registry")
        if schema.get("version") != 1 or schema.get("feature_order") != list(FEATURE_NAMES):
            raise ValueError("Unsupported Registry feature schema")
        logged = Model.load(str(root / "mlflow_model"))
        signature = logged.signature
        if signature is None or signature.inputs is None or [item.name for item in signature.inputs.inputs] != list(FEATURE_NAMES):
            raise ValueError("MLflow model signature differs from feature contract")
        if [str(item.type) for item in signature.inputs.inputs] != ["DataType.long", "DataType.long", "DataType.integer", "DataType.integer", "DataType.integer"]:
            raise ValueError("MLflow model signature input types differ from feature contract")
        if signature.outputs is None or len(signature.outputs.inputs) != 1:
            raise ValueError("MLflow model signature lacks one prediction output")
        if str(signature.outputs.inputs[0].type) != "float64":
            raise ValueError("MLflow model signature output must be float64")
        if logged.metadata is None or logged.metadata.get("feature_schema") != schema:
            raise ValueError("MLflow model metadata differs from training feature schema")
        if "lightgbm" not in logged.flavors:
            raise ValueError("Registry model lacks LightGBM flavor")
        sample = {"tpep_pickup_datetime": "2025-04-07T12:00:00", "PULocationID": 1, "DOLocationID": 2}
        features = preprocess_request(sample)
        registered_model = mlflow.lightgbm.load_model(str(root / "mlflow_model"))
        native = load_model(root).predict(sample)
        if not np.isclose(registered_model.predict(features)[0], native, rtol=1e-10, atol=1e-10):
            raise ValueError("Native artifact prediction differs from Registry model")
        shutil.rmtree(root / "downloads")
        files = inventory(root)
        checksum = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        metadata = {
            "format_version": 1,
            "model_name": name,
            "model_version": version,
            "approval_status": "approved",
            "mlflow_run_id": run_id,
            "source_run_id": tags["source_run_id"],
            "git_sha": tags["git_sha"],
            "artifact_checksum": checksum,
            "validation_dataset_key": tags["validation_dataset_key"],
            "validation_metric_definition": tags["validation_metric_definition"],
            "validation_mae_minutes": float(tags["validation_mae_minutes"]),
            "feature_order": list(FEATURE_NAMES),
            "files": files,
        }
        (root / "release.json").write_text(json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        validate_release(root)
        shutil.move(str(root), output)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", default="http://127.0.0.1:5000")
    parser.add_argument("--model-name", default="nyc-taxi-trip-duration")
    parser.add_argument("--model-version", default="1")
    parser.add_argument("--output", type=Path, default=Path("artifacts/releases/nyc-taxi-trip-duration-v1"))
    args = parser.parse_args()
    metadata = export_model(args.tracking_uri, args.model_name, args.model_version, args.output)
    print(json.dumps({key: metadata[key] for key in ("model_name", "model_version", "source_run_id", "git_sha", "artifact_checksum")}, sort_keys=True))


if __name__ == "__main__":
    main()
