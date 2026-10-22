"""Pure validation decisions; test MAE never participates in registration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable


METRIC_DEFINITION = "validation_mae_minutes:v1:mean_abs_error:cleaned_seeded_month_sample"


@dataclass(frozen=True)
class Reference:
    kind: str
    mae_minutes: float
    version: str | None = None


@dataclass(frozen=True)
class ApprovedVersion:
    version: str
    run_id: str
    dataset_key: str
    metric_definition: str
    mae_minutes: float
    approved: bool = True


def validation_dataset_key(manifest: dict[str, Any], schema: dict[str, Any]) -> str:
    """Identify the exact validation sample and metric preprocessing contract."""
    config = manifest["configuration"]
    months = config["periods"]["validation"]
    identity = {
        "months": [
            {
                "period": month,
                "sha256": manifest["datasets"][month]["sha256"],
                "rows_after_sampling": manifest["datasets"][month]["rows_after_sampling"],
            }
            for month in months
        ],
        "seed": config["seed"],
        "sample_limit_per_month": config["sample_limit_per_month"],
        "duration_min_minutes": config["duration_min_minutes"],
        "duration_max_minutes": config["duration_max_minutes"],
        "feature_schema": schema,
        "metric_definition": METRIC_DEFINITION,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def find_duplicate(versions: Iterable[Any], run_id: str, source_run_id: str) -> Any | None:
    return next(
        (
            version for version in versions
            if version.run_id == run_id or (version.tags or {}).get("source_run_id") == source_run_id
        ),
        None,
    )


def select_reference(
    *,
    dataset_key: str,
    baseline_mae: float,
    champion: ApprovedVersion | None,
    versions: Iterable[ApprovedVersion],
) -> Reference:
    if champion is not None:
        if champion.dataset_key != dataset_key or champion.metric_definition != METRIC_DEFINITION:
            raise ValueError("Champion validation dataset or metric is incompatible; re-evaluate before comparison")
        return Reference("champion", champion.mae_minutes, champion.version)

    approved = [version for version in versions if version.approved]
    compatible = [
        version for version in approved
        if version.dataset_key == dataset_key and version.metric_definition == METRIC_DEFINITION
    ]
    if compatible:
        best = min(compatible, key=lambda version: version.mae_minutes)
        return Reference("best_approved_candidate", best.mae_minutes, best.version)
    if approved:
        raise ValueError("Existing approved candidates use an incompatible validation dataset or metric")
    return Reference("median_baseline", baseline_mae)


def passes_gate(candidate_mae: float, reference: Reference, minimum_improvement_fraction: float) -> bool:
    if not 0 <= minimum_improvement_fraction < 1:
        raise ValueError("minimum_improvement_fraction must be between 0 and 1")
    if not all(math.isfinite(value) and value > 0 for value in (candidate_mae, reference.mae_minutes)):
        raise ValueError("Validation MAE and reference MAE must be positive finite numbers")
    return candidate_mae <= reference.mae_minutes * (1 - minimum_improvement_fraction)
