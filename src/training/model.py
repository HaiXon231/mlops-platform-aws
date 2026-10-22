"""CPU LightGBM training, evaluation, artifact writing and inference loading."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any, Mapping

import lightgbm as lgb
import numpy as np
import pandas as pd

from training.config import TrainingConfig
from training.data import inspect_month, sample_month
from training.features import FEATURE_NAMES, TARGET_NAME, feature_schema, preprocess_request


def _metrics(truth: pd.Series, predictions: np.ndarray) -> dict[str, float]:
    error = truth.to_numpy(dtype="float64") - np.asarray(predictions, dtype="float64")
    return {
        "mae_minutes": float(np.mean(np.abs(error))),
        "rmse_minutes": float(np.sqrt(np.mean(np.square(error)))),
    }


def _git_sha() -> str | None:
    # Images omit .git; the build passes the source revision explicitly.
    if image_revision := os.getenv("GIT_SHA"):
        return image_revision
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def train(config: TrainingConfig) -> Path:
    """Train once from fixed time periods; use validation only for early stopping."""
    per_month: dict[str, pd.DataFrame] = {}
    dataset_manifest: dict[str, dict[str, Any]] = {}
    for period in config.all_months:
        metadata = inspect_month(config, period)
        sampled, counts = sample_month(config, period)
        per_month[period] = sampled
        dataset_manifest[period] = {**metadata, **counts}

    def split(months: tuple[str, ...]) -> pd.DataFrame:
        return pd.concat([per_month[month] for month in months], ignore_index=True)

    train_set = split(config.train_months)
    validation_set = split(config.validation_months)
    test_set = split(config.test_months)
    x_train, y_train = train_set[list(FEATURE_NAMES)], train_set[TARGET_NAME]
    x_validation, y_validation = validation_set[list(FEATURE_NAMES)], validation_set[TARGET_NAME]
    x_test, y_test = test_set[list(FEATURE_NAMES)], test_set[TARGET_NAME]

    regressor = lgb.LGBMRegressor(**config.model_params, random_state=config.seed, device_type="cpu")
    regressor.fit(
        x_train,
        y_train,
        eval_X=x_validation,
        eval_y=y_validation,
        eval_metric="l1",
        categorical_feature=list(FEATURE_NAMES[:2]),
        callbacks=[lgb.early_stopping(config.early_stopping_rounds, verbose=False)],
    )
    baseline_median = float(np.median(y_train.to_numpy()))

    def evaluate(x: pd.DataFrame, y: pd.Series) -> dict[str, Any]:
        model_metrics = _metrics(y, regressor.predict(x))
        baseline_metrics = _metrics(y, np.full(len(y), baseline_median))
        return {
            "rows": len(y),
            "model": model_metrics,
            "baseline_median": baseline_metrics,
            "mae_improvement_over_baseline_minutes": baseline_metrics["mae_minutes"] - model_metrics["mae_minutes"],
        }

    metrics = {
        "primary_metric": "mae_minutes",
        "secondary_metric": "rmse_minutes",
        "baseline_train_median_minutes": baseline_median,
        "validation": evaluate(x_validation, y_validation),
        "test": evaluate(x_test, y_test),
    }
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = config.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    regressor.booster_.save_model(str(run_dir / "model.txt"), num_iteration=regressor.best_iteration_)
    _write_json(run_dir / "metrics.json", metrics)
    _write_json(run_dir / "feature_schema.json", feature_schema(config.duration_min_minutes, config.duration_max_minutes))
    manifest = {
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "source_tree_sha256": os.getenv("SOURCE_TREE_SHA256"),
        "dependencies": {
            "python": platform.python_version(),
            **{package: version(package) for package in ("boto3", "lightgbm", "numpy", "pandas", "pyarrow", "PyYAML", "scikit-learn")},
        },
        "configuration": config.as_manifest_dict(),
        "datasets": dataset_manifest,
        "split_rows": {
            "train": len(train_set),
            "validation": len(validation_set),
            "test": len(test_set),
        },
        "best_iteration": regressor.best_iteration_,
        "artifacts": ["model.txt", "metrics.json", "feature_schema.json", "manifest.json"],
    }
    _write_json(run_dir / "manifest.json", manifest)
    return run_dir


@dataclass(frozen=True)
class LoadedModel:
    booster: lgb.Booster
    schema: dict[str, Any]

    def predict(self, request: Mapping[str, Any]) -> float:
        features = preprocess_request(request)
        if list(features.columns) != self.schema["feature_order"]:
            raise ValueError("Inference feature order does not match the saved model contract")
        return float(self.booster.predict(features)[0])


def load_model(run_dir: Path) -> LoadedModel:
    schema = json.loads((run_dir / "feature_schema.json").read_text(encoding="utf-8"))
    if schema.get("version") != 1 or schema.get("feature_order") != list(FEATURE_NAMES):
        raise ValueError("Unsupported feature schema or feature order")
    return LoadedModel(lgb.Booster(model_file=str(run_dir / "model.txt")), schema)
