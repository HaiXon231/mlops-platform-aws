"""Validate the offline release before loading its native LightGBM model."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from training.features import FEATURE_NAMES
from training.model import LoadedModel, load_model

REQUIRED_FILES = ("model.txt", "feature_schema.json", "training_manifest.json", "mlflow_model/MLmodel")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): file_hash(path) for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != "release.json"}


def validate_release(root: Path) -> tuple[dict, LoadedModel]:
    metadata = json.loads((root / "release.json").read_text(encoding="utf-8"))
    if metadata.get("format_version") != 1 or metadata.get("approval_status") != "approved":
        raise ValueError("Release metadata is unsupported or unapproved")
    if not all(isinstance(metadata.get(key), str) and metadata[key] for key in
               ("model_name", "model_version", "mlflow_run_id", "source_run_id", "git_sha", "artifact_checksum")):
        raise ValueError("Release provenance is incomplete")
    files = metadata.get("files")
    if not isinstance(files, dict) or any(name not in files for name in REQUIRED_FILES):
        raise ValueError("Release file inventory is incomplete")
    actual = inventory(root)
    if files != actual:
        raise ValueError("Release artifact checksum mismatch")
    overall = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if metadata["artifact_checksum"] != overall:
        raise ValueError("Release artifact checksum mismatch")
    schema = json.loads((root / "feature_schema.json").read_text(encoding="utf-8"))
    if schema.get("feature_order") != list(FEATURE_NAMES) or [f.get("dtype") for f in schema.get("features", [])] != ["category", "category", "uint8", "uint8", "uint8"]:
        raise ValueError("Release feature schema differs from training")
    manifest = json.loads((root / "training_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("run_id") != metadata["source_run_id"] or (manifest.get("git_sha") or "unknown") != metadata["git_sha"]:
        raise ValueError("Release provenance differs from training manifest")
    model = load_model(root)
    if not math.isfinite(model.predict({"tpep_pickup_datetime": "2025-04-07T12:00:00", "PULocationID": 1, "DOLocationID": 2})):
        raise ValueError("Model smoke prediction is not finite")
    return metadata, model
