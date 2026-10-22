"""Validated, file-based training configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TrainingConfig:
    seed: int
    train_months: tuple[str, ...]
    validation_months: tuple[str, ...]
    test_months: tuple[str, ...]
    data_dir: Path
    output_dir: Path
    source_base_url: str
    sample_limit_per_month: int
    parquet_batch_size: int
    duration_min_minutes: float
    duration_max_minutes: float
    early_stopping_rounds: int
    model_params: dict[str, Any]

    @property
    def all_months(self) -> tuple[str, ...]:
        return self.train_months + self.validation_months + self.test_months

    def with_paths(self, *, data_dir: Path | None = None, output_dir: Path | None = None) -> TrainingConfig:
        return replace(
            self,
            data_dir=data_dir.resolve() if data_dir is not None else self.data_dir,
            output_dir=output_dir.resolve() if output_dir is not None else self.output_dir,
        )

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "periods": {
                "train": list(self.train_months),
                "validation": list(self.validation_months),
                "test": list(self.test_months),
            },
            "data_dir": str(self.data_dir),
            "output_dir": str(self.output_dir),
            "source_base_url": self.source_base_url,
            "sample_limit_per_month": self.sample_limit_per_month,
            "parquet_batch_size": self.parquet_batch_size,
            "duration_min_minutes": self.duration_min_minutes,
            "duration_max_minutes": self.duration_max_minutes,
            "early_stopping_rounds": self.early_stopping_rounds,
            "model": self.model_params,
        }


def load_config(path: Path) -> TrainingConfig:
    path = path.resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Training config must be a YAML mapping")
    periods = raw["periods"]
    if not isinstance(periods, dict):
        raise ValueError("periods must be a mapping")

    def months(name: str) -> tuple[str, ...]:
        values = periods[name]
        if not isinstance(values, list) or not values:
            raise ValueError(f"periods.{name} must be a non-empty list")
        result = tuple(values)
        if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) for value in result):
            raise ValueError(f"periods.{name} must contain YYYY-MM strings")
        return result

    train, validation, test = months("train"), months("validation"), months("test")
    all_months = train + validation + test
    if len(all_months) != len(set(all_months)) or not (max(train) < min(validation) <= max(validation) < min(test)):
        raise ValueError("Train, validation and test months must be distinct and chronological")

    def local_path(key: str) -> Path:
        value = Path(raw[key])
        return (value if value.is_absolute() else path.parent / value).resolve()

    model_params = raw["model"]
    if not isinstance(model_params, dict):
        raise ValueError("model must be a mapping of LightGBM hyperparameters")
    reserved = {"device", "device_type", "random_state", "seed", "data_random_seed", "feature_fraction_seed", "bagging_seed"}
    if reserved & set(model_params):
        raise ValueError(f"Model parameters {sorted(reserved & set(model_params))} are managed by the CPU training pipeline")

    config = TrainingConfig(
        seed=int(raw["seed"]),
        train_months=train,
        validation_months=validation,
        test_months=test,
        data_dir=local_path("data_dir"),
        output_dir=local_path("output_dir"),
        source_base_url=str(raw["source_base_url"]).rstrip("/"),
        sample_limit_per_month=int(raw["sample_limit_per_month"]),
        parquet_batch_size=int(raw["parquet_batch_size"]),
        duration_min_minutes=float(raw["duration_min_minutes"]),
        duration_max_minutes=float(raw["duration_max_minutes"]),
        early_stopping_rounds=int(raw["early_stopping_rounds"]),
        model_params=dict(model_params),
    )
    if config.seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if config.sample_limit_per_month < 1 or config.parquet_batch_size < 1:
        raise ValueError("sample_limit_per_month and parquet_batch_size must be positive")
    if not 0 < config.duration_min_minutes < config.duration_max_minutes:
        raise ValueError("Require 0 < duration_min_minutes < duration_max_minutes")
    if config.early_stopping_rounds < 1:
        raise ValueError("early_stopping_rounds must be positive")
    if not config.source_base_url.startswith("https://"):
        raise ValueError("source_base_url must use HTTPS")
    return config
