from datetime import datetime

import pandas as pd
import pytest

from training.features import (
    FEATURE_NAMES,
    TARGET_NAME,
    build_features,
    clean_month_batch,
    preprocess_request,
)


def row(pickup="2025-01-15 08:30:00", dropoff="2025-01-15 08:45:00", pu=1, do=2):
    return {
        "tpep_pickup_datetime": pickup,
        "tpep_dropoff_datetime": dropoff,
        "PULocationID": pu,
        "DOLocationID": do,
    }


def test_duration_in_minutes_and_feature_order():
    cleaned = clean_month_batch(pd.DataFrame([row()]), "2025-01", 1, 180)
    assert cleaned[TARGET_NAME].tolist() == [15.0]
    assert cleaned.columns.tolist() == [*FEATURE_NAMES, TARGET_NAME]
    assert cleaned["pickup_hour"].tolist() == [8]
    assert cleaned["pickup_day_of_week"].tolist() == [2]  # Wednesday
    assert cleaned["pickup_month"].tolist() == [1]
    assert str(cleaned["PULocationID"].dtype) == "category"
    assert list(cleaned["PULocationID"].cat.categories) == list(range(1, 264))


def test_cleaning_drops_invalid_duration_location_and_missing_values():
    rows = [
        row(),
        row(dropoff="2025-01-15 08:29:00"),
        row(dropoff="2025-01-15 08:30:30"),
        row(dropoff="2025-01-15 12:00:00"),
        row(pu=None),
        row(do=0),
        row(pu=1.5),
        row(pickup=None),
        row(dropoff=None),
    ]
    cleaned = clean_month_batch(pd.DataFrame(rows), "2025-01", 1, 180)
    assert len(cleaned) == 1
    assert cleaned[TARGET_NAME].iloc[0] == 15


def test_pickup_must_belong_to_file_month_even_if_dropoff_is_valid():
    rows = [
        row(pickup="2024-12-31 23:55:00", dropoff="2025-01-01 00:05:00"),
        row(pickup="2025-02-01 00:00:00", dropoff="2025-02-01 00:10:00"),
        row(pickup="2025-01-31 23:55:00", dropoff="2025-02-01 00:05:00"),
    ]
    cleaned = clean_month_batch(pd.DataFrame(rows), "2025-01", 1, 180)
    assert len(cleaned) == 1
    assert cleaned[TARGET_NAME].iloc[0] == 10


def test_inference_uses_same_schema_and_rejects_missing_or_post_trip_fields():
    request = {
        "PULocationID": 1,
        "DOLocationID": 2,
        "tpep_pickup_datetime": "2025-04-07T21:00:00",
    }
    features = preprocess_request(request)
    assert features.columns.tolist() == list(FEATURE_NAMES)
    assert features["pickup_day_of_week"].iloc[0] == 0
    assert features["pickup_month"].iloc[0] == 4
    assert features.equals(build_features(pd.DataFrame([request])))
    with pytest.raises(ValueError, match="exactly"):
        preprocess_request({**request, "tpep_dropoff_datetime": "2025-04-07T21:20:00"})
    with pytest.raises(ValueError, match="exactly"):
        preprocess_request({"PULocationID": 1, "tpep_pickup_datetime": "2025-04-07T21:00:00"})
    with pytest.raises(ValueError, match="taxi zone"):
        preprocess_request({**request, "DOLocationID": None})
    with pytest.raises(ValueError, match="Timezone-aware"):
        preprocess_request({**request, "tpep_pickup_datetime": datetime.fromisoformat("2025-04-07T21:00:00+07:00")})
