"""Use a real local M3.1 bundle when available to check runner release metadata."""

import importlib.util
import shutil
from pathlib import Path

import pytest

from inference.bundle import validate_release

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "artifacts/releases/nyc-taxi-trip-duration-v1"
SPEC = importlib.util.spec_from_file_location("m32_gitops", ROOT / "scripts/m32_gitops.py")
gitops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gitops)


def test_real_bundle_prepared_for_runner_and_prediction_unchanged(tmp_path):
    if not SOURCE.exists():
        pytest.skip("Approved local M3.1 bundle unavailable")
    original, original_model = validate_release(SOURCE)
    bundle = tmp_path / "bundle"
    shutil.copytree(SOURCE, bundle)
    release = gitops.prepare_bundle(bundle, "m32-123-1", "a" * 40)
    checked, checked_model = validate_release(bundle)
    sample = {"tpep_pickup_datetime": "2025-04-07T12:00:00", "PULocationID": 1, "DOLocationID": 2}
    assert release["artifact_checksum"] == original["artifact_checksum"]
    assert checked["release_id"] == "m32-123-1"
    assert checked["serving_git_sha"] == "a" * 40
    assert checked_model.predict(sample) == original_model.predict(sample)
