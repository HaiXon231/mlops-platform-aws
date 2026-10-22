import io
import json

import pandas as pd
import pytest

from training.cloud import fetch_months, keys, sync_months
from training.config import load_config


class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")
        data, metadata = self.objects[Bucket, Key]
        return {"ContentLength": len(data), "Metadata": metadata}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        self.objects[Bucket, Key] = (open(Filename, "rb").read(), (ExtraArgs or {}).get("Metadata", {}))

    def download_file(self, Bucket, Key, Filename):
        open(Filename, "wb").write(self.objects[Bucket, Key][0])

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Bucket, Key][0])}


def test_dataset_sync_keeps_provenance_and_checks_checksum(tmp_path):
    config = load_config(__import__("pathlib").Path("configs/training.yaml")).with_paths(data_dir=tmp_path / "input")
    config.data_dir.mkdir()
    for period in config.all_months:
        frame = pd.DataFrame({
            "tpep_pickup_datetime": [pd.Timestamp(f"{period}-01 12:00")],
            "tpep_dropoff_datetime": [pd.Timestamp(f"{period}-01 12:10")],
            "PULocationID": [1], "DOLocationID": [2],
        })
        frame.to_parquet(config.data_dir / f"yellow_tripdata_{period}.parquet", index=False)
    s3 = FakeS3()
    uploaded = sync_months(s3, "datasets", config)
    assert len(uploaded) == 4
    assert all(not row["source_verified"] for row in uploaded)
    assert sync_months(s3, "datasets", config) == uploaded

    fetched = fetch_months(s3, "datasets", config.with_paths(data_dir=tmp_path / "pod"))
    assert len(fetched) == 4
    assert all(not row["source_verified"] for row in fetched)

    data_key, _ = keys("2025-01")
    data, metadata = s3.objects["datasets", data_key]
    s3.objects["datasets", data_key] = (data, {**metadata, "sha256": "bad"})
    with pytest.raises(ValueError, match="different SHA256"):
        sync_months(s3, "datasets", config)
