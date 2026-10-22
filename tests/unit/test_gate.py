from types import SimpleNamespace

import pytest

from training.gate import (
    METRIC_DEFINITION,
    ApprovedVersion,
    Reference,
    find_duplicate,
    passes_gate,
    select_reference,
    validation_dataset_key,
)


def _version(version="1", dataset="same", mae=8.0, run_id="first"):
    return ApprovedVersion(version, run_id, dataset, METRIC_DEFINITION, mae)


def test_no_reference_uses_median_baseline_and_passes():
    reference = select_reference(dataset_key="same", baseline_mae=10, champion=None, versions=[])
    assert reference == Reference("median_baseline", 10)
    assert passes_gate(9.8, reference, 0.01)


def test_rejects_candidate_below_minimum_improvement():
    assert not passes_gate(9.95, Reference("median_baseline", 10), 0.01)


def test_threshold_boundary_is_inclusive():
    assert passes_gate(99, Reference("champion", 100, "2"), 0.01)
    assert not passes_gate(99.001, Reference("champion", 100, "2"), 0.01)


def test_champion_has_priority_over_best_approved_candidate():
    reference = select_reference(
        dataset_key="same", baseline_mae=10, champion=_version("4", mae=9), versions=[_version("2", mae=8)]
    )
    assert reference == Reference("champion", 9, "4")


def test_best_approved_candidate_without_champion():
    reference = select_reference(
        dataset_key="same", baseline_mae=10, champion=None, versions=[_version("1", mae=8), _version("2", mae=7)]
    )
    assert reference == Reference("best_approved_candidate", 7, "2")


def test_incompatible_champion_or_candidates_are_blocked():
    with pytest.raises(ValueError, match="Champion validation dataset"):
        select_reference(dataset_key="same", baseline_mae=10, champion=_version(dataset="other"), versions=[])
    with pytest.raises(ValueError, match="incompatible validation dataset"):
        select_reference(dataset_key="same", baseline_mae=10, champion=None, versions=[_version(dataset="other")])
    with pytest.raises(ValueError, match="Champion validation dataset"):
        select_reference(
            dataset_key="same", baseline_mae=10,
            champion=ApprovedVersion("1", "run", "same", "different_metric", 8), versions=[],
        )


def test_duplicate_registration_matches_run_or_source_run():
    versions = [SimpleNamespace(version="3", run_id="tracked", tags={"source_run_id": "local"})]
    assert find_duplicate(versions, "tracked", "other").version == "3"
    assert find_duplicate(versions, "other", "local").version == "3"
    assert find_duplicate(versions, "other", "unseen") is None


def test_validation_key_uses_validation_data_not_test_data():
    manifest = {
        "configuration": {
            "periods": {"validation": ["2025-03"]}, "seed": 42,
            "sample_limit_per_month": 100, "duration_min_minutes": 1, "duration_max_minutes": 180,
        },
        "datasets": {"2025-03": {"sha256": "a", "rows_after_sampling": 100}, "2025-04": {"sha256": "b"}},
    }
    first = validation_dataset_key(manifest, {"version": 1})
    manifest["datasets"]["2025-04"]["sha256"] = "changed"
    assert validation_dataset_key(manifest, {"version": 1}) == first
    manifest["datasets"]["2025-03"]["sha256"] = "changed"
    assert validation_dataset_key(manifest, {"version": 1}) != first
