"""Optional MLflow tracking, validation gate and candidate registration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mlflow
import mlflow.lightgbm
import yaml
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException
from mlflow.models import infer_signature

from training.features import preprocess_request
from training.gate import (
    METRIC_DEFINITION,
    ApprovedVersion,
    find_duplicate,
    passes_gate,
    select_reference,
    validation_dataset_key,
)
from training.model import load_model


@dataclass(frozen=True)
class TrackingConfig:
    experiment_name: str
    registered_model_name: str
    minimum_mae_improvement_fraction: float


def load_tracking_config(path: Path) -> TrackingConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("MLflow config must be a YAML mapping")
    config = TrackingConfig(
        experiment_name=str(raw["experiment_name"]),
        registered_model_name=str(raw["registered_model_name"]),
        minimum_mae_improvement_fraction=float(raw["minimum_mae_improvement_fraction"]),
    )
    if not config.experiment_name or not config.registered_model_name:
        raise ValueError("MLflow experiment and registered model names are required")
    if not 0 <= config.minimum_mae_improvement_fraction < 1:
        raise ValueError("Minimum MAE improvement fraction must be between 0 and 1")
    return config


def _read_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    required = ("manifest.json", "metrics.json", "feature_schema.json", "model.txt")
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Training run {run_dir} is missing: {', '.join(missing)}")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    schema = json.loads((run_dir / "feature_schema.json").read_text(encoding="utf-8"))
    if metrics.get("primary_metric") != "mae_minutes" or metrics.get("secondary_metric") != "rmse_minutes":
        raise ValueError("Unsupported metrics definition in training run")
    if manifest.get("run_id") != run_dir.name:
        raise ValueError("Training run directory and manifest run_id differ")
    for split in ("validation", "test"):
        for source in ("model", "baseline_median"):
            for metric in ("mae_minutes", "rmse_minutes"):
                value = metrics[split][source][metric]
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"Invalid {split}.{source}.{metric}: {value}")
    return manifest, metrics, schema


def _approved_version(version: Any, *, required: bool = False) -> ApprovedVersion | None:
    tags = version.tags or {}
    if tags.get("validation_status") != "approved":
        if required:
            raise ValueError("Champion lacks approved validation metadata")
        return None
    try:
        return ApprovedVersion(
            version=str(version.version),
            run_id=str(version.run_id),
            dataset_key=tags["validation_dataset_key"],
            metric_definition=tags["validation_metric_definition"],
            mae_minutes=float(tags["validation_mae_minutes"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Model version {version.version} lacks valid validation metadata") from error


def _versions(client: MlflowClient, name: str) -> list[Any]:
    return list(client.search_model_versions(f"name = '{name}'"))


def _champion(client: MlflowClient, name: str) -> ApprovedVersion | None:
    try:
        version = client.get_model_version_by_alias(name, "champion")
    except MlflowException as error:
        if error.error_code == "RESOURCE_DOES_NOT_EXIST" or (
            error.error_code == "INVALID_PARAMETER_VALUE" and "alias champion not found" in str(error).lower()
        ):
            return None
        raise
    return _approved_version(version, required=True)


def _gate_context(
    client: MlflowClient,
    config: TrackingConfig,
    manifest: dict[str, Any],
    metrics: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    dataset_key = validation_dataset_key(manifest, schema)
    all_versions = _versions(client, config.registered_model_name)
    reference = select_reference(
        dataset_key=dataset_key,
        baseline_mae=float(metrics["validation"]["baseline_median"]["mae_minutes"]),
        champion=_champion(client, config.registered_model_name),
        versions=[approved for version in all_versions if (approved := _approved_version(version)) is not None],
    )
    candidate_mae = float(metrics["validation"]["model"]["mae_minutes"])
    return {
        "passed": passes_gate(candidate_mae, reference, config.minimum_mae_improvement_fraction),
        "candidate_validation_mae_minutes": candidate_mae,
        "reference_type": reference.kind,
        "reference_version": reference.version,
        "reference_validation_mae_minutes": reference.mae_minutes,
        "minimum_improvement_fraction": config.minimum_mae_improvement_fraction,
        "validation_dataset_key": dataset_key,
        "validation_metric_definition": METRIC_DEFINITION,
    }


def _client(tracking_uri: str) -> MlflowClient:
    if not tracking_uri:
        raise ValueError("MLflow tracking URI is required")
    mlflow.set_tracking_uri(tracking_uri)
    return MlflowClient(tracking_uri=tracking_uri)


def preview_gate(run_dir: Path, tracking_uri: str, config: TrackingConfig) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest, metrics, schema = _read_run(run_dir)
    client = _client(tracking_uri)
    source_hash = hashlib.sha256((run_dir / "manifest.json").read_bytes()).hexdigest()
    existing = next(
        (
            version for version in _versions(client, config.registered_model_name)
            if (version.tags or {}).get("source_run_id") == manifest["run_id"]
        ),
        None,
    )
    if existing:
        tags = existing.tags or {}
        if tags.get("source_manifest_sha256") != source_hash:
            raise ValueError("A model version already uses this source run ID with different manifest content")
        dataset_key = validation_dataset_key(manifest, schema)
        if tags.get("validation_dataset_key") != dataset_key:
            raise ValueError("Registered model validation dataset differs from this run's feature schema")
        return {
            "passed": tags.get("validation_status") == "approved",
            "already_registered": True,
            "model_version": str(existing.version),
            "candidate_validation_mae_minutes": float(metrics["validation"]["model"]["mae_minutes"]),
            "reference_type": tags.get("reference_type"),
            "reference_validation_mae_minutes": float(tags["reference_validation_mae_minutes"]),
            "validation_dataset_key": dataset_key,
            "validation_metric_definition": METRIC_DEFINITION,
        }
    return _gate_context(client, config, manifest, metrics, schema)


def verify_tracking(tracking_uri: str) -> dict[str, Any]:
    client = _client(tracking_uri)
    experiments = client.search_experiments()
    models = client.search_registered_models()
    return {
        "tracking_uri": tracking_uri,
        "experiment_count": len(experiments),
        "registered_model_count": len(models),
        "status": "ok",
    }


def import_run(run_dir: Path, tracking_uri: str, config: TrackingConfig) -> dict[str, Any]:
    """Track a completed M2.1 run; register only after the validation gate passes."""
    run_dir = run_dir.resolve()
    manifest, metrics, schema = _read_run(run_dir)
    client = _client(tracking_uri)
    source_hash = hashlib.sha256((run_dir / "manifest.json").read_bytes()).hexdigest()
    source_run_id = manifest["run_id"]
    experiment = client.get_experiment_by_name(config.experiment_name)
    experiment_id = experiment.experiment_id if experiment else client.create_experiment(config.experiment_name)
    matches = client.search_runs(
        [experiment_id],
        filter_string=f"tags.source_manifest_sha256 = '{source_hash}'",
        max_results=2,
    )
    if len(matches) > 1:
        raise ValueError("Multiple MLflow runs match the same source manifest; resolve duplicates manually")

    if matches:
        run_id = matches[0].info.run_id
        model_uri = matches[0].data.tags.get("logged_model_uri")
        if not model_uri:
            raise ValueError(f"MLflow run {run_id} is incomplete; inspect it before retrying import")
    else:
        loaded = load_model(run_dir)
        sample_month = manifest["configuration"]["periods"]["validation"][0]
        example = preprocess_request(
            {"tpep_pickup_datetime": f"{sample_month}-01T12:00:00", "PULocationID": 1, "DOLocationID": 2}
        )
        signature = infer_signature(example, loaded.booster.predict(example))
        with mlflow.start_run(experiment_id=experiment_id, run_name=source_run_id) as active:
            run_id = active.info.run_id
            params = {
                "seed": manifest["configuration"]["seed"],
                "sample_limit_per_month": manifest["configuration"]["sample_limit_per_month"],
                "train_months": ",".join(manifest["configuration"]["periods"]["train"]),
                "validation_months": ",".join(manifest["configuration"]["periods"]["validation"]),
                "test_months": ",".join(manifest["configuration"]["periods"]["test"]),
                **{f"model.{key}": value for key, value in manifest["configuration"]["model"].items()},
            }
            mlflow.log_params(params)
            mlflow.log_metrics(
                {
                    f"{split}.{source}.{metric}": metrics[split][source][metric]
                    for split in ("validation", "test")
                    for source in ("model", "baseline_median")
                    for metric in ("mae_minutes", "rmse_minutes")
                }
            )
            mlflow.set_tags(
                {
                    "source_run_id": source_run_id,
                    "source_manifest_sha256": source_hash,
                    "git_sha": manifest.get("git_sha") or "unknown",
                    "dataset_source_verified": str(all(item["source_verified"] for item in manifest["datasets"].values())).lower(),
                    "validation_dataset_key": validation_dataset_key(manifest, schema),
                    "validation_metric_definition": METRIC_DEFINITION,
                }
            )
            mlflow.log_artifacts(str(run_dir), artifact_path="training")
            logged = mlflow.lightgbm.log_model(
                loaded.booster,
                name="model",
                signature=signature,
                metadata={"feature_schema": schema},
                pip_requirements=[
                    f"mlflow=={mlflow.__version__}",
                    f"lightgbm=={manifest['dependencies']['lightgbm']}",
                    f"numpy=={manifest['dependencies']['numpy']}",
                    f"pandas=={manifest['dependencies']['pandas']}",
                ],
            )
            model_uri = logged.model_uri
            mlflow.set_tag("logged_model_uri", model_uri)

    versions = _versions(client, config.registered_model_name)
    duplicate = find_duplicate(versions, run_id, source_run_id)
    if duplicate:
        if (duplicate.tags or {}).get("source_manifest_sha256") != source_hash:
            raise ValueError("A model version already uses this source run ID with different manifest content")
        return {"mlflow_run_id": run_id, "validation_status": "approved", "model_version": str(duplicate.version), "already_registered": True}

    try:
        decision = _gate_context(client, config, manifest, metrics, schema)
    except ValueError:
        client.set_tag(run_id, "validation_status", "blocked_incompatible_dataset")
        raise
    if not decision["passed"]:
        client.set_tag(run_id, "validation_status", "rejected")
        return {"mlflow_run_id": run_id, "validation_status": "rejected", "model_version": None, **decision}

    try:
        client.create_registered_model(config.registered_model_name)
    except MlflowException as error:
        if error.error_code != "RESOURCE_ALREADY_EXISTS":
            raise
    tags = {
        "validation_status": "approved",
        "validation_dataset_key": decision["validation_dataset_key"],
        "validation_metric_definition": METRIC_DEFINITION,
        "validation_mae_minutes": str(decision["candidate_validation_mae_minutes"]),
        "reference_type": decision["reference_type"],
        "reference_validation_mae_minutes": str(decision["reference_validation_mae_minutes"]),
        "source_run_id": source_run_id,
        "source_manifest_sha256": source_hash,
        "dataset_source_verified": str(all(item["source_verified"] for item in manifest["datasets"].values())).lower(),
        "git_sha": manifest.get("git_sha") or "unknown",
    }
    version = client.create_model_version(
        config.registered_model_name, source=model_uri, run_id=run_id, tags=tags
    )
    client.set_tag(run_id, "validation_status", "approved")
    client.set_tag(run_id, "registered_model_version", str(version.version))
    return {"mlflow_run_id": run_id, "validation_status": "approved", "model_version": str(version.version), **decision}
