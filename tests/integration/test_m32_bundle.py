"""S3 transfer parity using the real exported M3.1 bundle when present."""

import io
import json
import shutil
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from inference import release_s3
from inference.bundle import validate_release

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "artifacts/releases/nyc-taxi-trip-duration-v1"


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def upload_file(self, Filename, Bucket, Key):
        self.objects[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[Key])

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body


def test_real_bundle_s3_round_trip_and_prediction_parity(monkeypatch, tmp_path):
    if not SOURCE.exists():
        pytest.skip("Approved local M3.1 bundle unavailable")
    original, model = validate_release(SOURCE)

    def copy_export(uri, name, version, output):
        assert name == original["model_name"] and version == original["model_version"]
        shutil.copytree(SOURCE, output)
        return json.loads((output / "release.json").read_text())

    monkeypatch.setattr(release_s3, "export_model", copy_export)
    bucket = "mlops-aws-model-artifacts-dev-160616607747-ap-southeast-1"
    serving_sha = "a" * 40
    s3 = MemoryS3()
    completion = release_s3.export_to_s3(bucket, original["model_name"], original["model_version"],
                                          "m32-123", serving_sha, "http://unused", s3)
    downloaded = tmp_path / "downloaded"
    assert release_s3.download_from_s3(bucket, original["model_name"], original["model_version"],
                                       "m32-123", serving_sha, downloaded, s3) == completion
    copied, copied_model = validate_release(downloaded)
    request = {"tpep_pickup_datetime": "2025-04-07T12:00:00", "PULocationID": 1, "DOLocationID": 2}
    assert copied["artifact_checksum"] == original["artifact_checksum"]
    assert copied_model.predict(request) == model.predict(request)
