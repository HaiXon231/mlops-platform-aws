"""Shared training/serving feature contract and leakage-safe preprocessing."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

import numpy as np
import pandas as pd

PICKUP_COLUMN = "tpep_pickup_datetime"
DROPOFF_COLUMN = "tpep_dropoff_datetime"
LOCATION_COLUMNS = ("PULocationID", "DOLocationID")
RAW_COLUMNS = (PICKUP_COLUMN, DROPOFF_COLUMN, *LOCATION_COLUMNS)
FEATURE_NAMES = ("PULocationID", "DOLocationID", "pickup_hour", "pickup_day_of_week", "pickup_month")
TARGET_NAME = "duration_minutes"
LOCATION_CATEGORIES = list(range(1, 264))


def feature_schema(min_minutes: float, max_minutes: float) -> dict[str, Any]:
    return {
        "version": 1,
        "target": {"name": TARGET_NAME, "unit": "minutes", "training_only": True},
        "inference_inputs": {
            PICKUP_COLUMN: "naive NYC local timestamp (ISO 8601 or datetime)",
            "PULocationID": "integer taxi zone ID 1..263",
            "DOLocationID": "integer taxi zone ID 1..263",
        },
        "features": [
            {"name": "PULocationID", "dtype": "category", "categories": "integer 1..263"},
            {"name": "DOLocationID", "dtype": "category", "categories": "integer 1..263"},
            {"name": "pickup_hour", "dtype": "uint8", "range": [0, 23]},
            {"name": "pickup_day_of_week", "dtype": "uint8", "range": [0, 6], "convention": "Monday=0"},
            {"name": "pickup_month", "dtype": "uint8", "range": [1, 12]},
        ],
        "feature_order": list(FEATURE_NAMES),
        "preprocessing": {
            "pickup_month_must_match_source_file": True,
            "duration_minutes": "(dropoff - pickup).total_seconds() / 60",
            "accepted_duration_minutes": [min_minutes, max_minutes],
            "location_ids": "non-null integers from 1 through 263; fixed pandas categorical domain",
            "timestamp_timezone": "NYC local wall time, without timezone offset",
            "excluded_features": [DROPOFF_COLUMN, "trip_distance", "fare_amount", "payment_type"],
        },
    }


def _valid_location(values: pd.Series) -> pd.Series:
    return values.notna() & values.between(1, 263) & values.mod(1).eq(0)


def build_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Make the exact LightGBM input from information known at pickup time."""
    required = {PICKUP_COLUMN, *LOCATION_COLUMNS}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing inference inputs: {', '.join(sorted(missing))}")
    pickup = pd.to_datetime(frame[PICKUP_COLUMN], errors="coerce")
    if pickup.isna().any() or pickup.dt.tz is not None:
        raise ValueError("Pickup timestamps must be valid naive NYC local datetimes")
    locations: dict[str, pd.Series] = {}
    for name in LOCATION_COLUMNS:
        values = pd.to_numeric(frame[name], errors="coerce")
        if not _valid_location(values).all():
            raise ValueError(f"{name} must be an integer taxi zone ID from 1 through 263")
        locations[name] = values.astype("int16")

    return pd.DataFrame(
        {
            "PULocationID": pd.Categorical(locations["PULocationID"], categories=LOCATION_CATEGORIES),
            "DOLocationID": pd.Categorical(locations["DOLocationID"], categories=LOCATION_CATEGORIES),
            "pickup_hour": pickup.dt.hour.astype("uint8").to_numpy(),
            "pickup_day_of_week": pickup.dt.dayofweek.astype("uint8").to_numpy(),
            "pickup_month": pickup.dt.month.astype("uint8").to_numpy(),
        },
        columns=list(FEATURE_NAMES),
    )


def clean_month_batch(
    frame: pd.DataFrame,
    period: str,
    min_minutes: float,
    max_minutes: float,
) -> pd.DataFrame:
    """Filter invalid rows, then return ordered features and the training-only target."""
    missing = set(RAW_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing parquet columns: {', '.join(sorted(missing))}")
    pickup = pd.to_datetime(frame[PICKUP_COLUMN], errors="coerce")
    dropoff = pd.to_datetime(frame[DROPOFF_COLUMN], errors="coerce")
    month_start = pd.Timestamp(f"{period}-01")
    next_month = month_start + pd.DateOffset(months=1)
    duration = (dropoff - pickup).dt.total_seconds() / 60.0
    pu = pd.to_numeric(frame["PULocationID"], errors="coerce")
    do = pd.to_numeric(frame["DOLocationID"], errors="coerce")
    valid = (
        pickup.notna()
        & dropoff.notna()
        & pickup.ge(month_start)
        & pickup.lt(next_month)
        & duration.between(min_minutes, max_minutes)
        & np.isfinite(duration)
        & _valid_location(pu)
        & _valid_location(do)
    )
    if not valid.any():
        return pd.DataFrame(columns=[*FEATURE_NAMES, TARGET_NAME])
    inputs = pd.DataFrame(
        {
            PICKUP_COLUMN: pickup.loc[valid].reset_index(drop=True),
            "PULocationID": pu.loc[valid].reset_index(drop=True),
            "DOLocationID": do.loc[valid].reset_index(drop=True),
        }
    )
    result = build_features(inputs)
    result[TARGET_NAME] = duration.loc[valid].to_numpy(dtype="float64")
    return result


def preprocess_request(request: Mapping[str, Any]) -> pd.DataFrame:
    """Serving entry point: no dropoff time or post-trip fields are accepted."""
    required = {PICKUP_COLUMN, *LOCATION_COLUMNS}
    if set(request) != required:
        raise ValueError(f"Inference requires exactly: {', '.join(sorted(required))}")
    raw = dict(request)
    if isinstance(raw[PICKUP_COLUMN], datetime) and raw[PICKUP_COLUMN].tzinfo is not None:
        raise ValueError("Timezone-aware timestamps are not supported; pass NYC local wall time")
    return build_features(pd.DataFrame([raw]))
