"""Canary observation tools: traffic-share maths, k6 target selection and dashboard wiring."""

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_split():
    spec = importlib.util.spec_from_file_location("m42_split", ROOT / "scripts/m42_split.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


split = load_split()


def response(**counts):
    return {"status": "success", "data": {"result": [
        {"metric": {"rollouts_pod_template_hash": key}, "value": [0, str(value)]}
        for key, value in counts.items()]}}


def test_share_reports_ninety_ten_between_stable_and_canary():
    result = split.shares(response(aaa=900, bbb=100), stable="aaa", current="bbb")
    rows = {row["hash"]: row for row in result["replicasets"]}
    assert rows["aaa"]["role"] == "stable" and rows["aaa"]["share_percent"] == 90.0
    assert rows["bbb"]["role"] == "canary" and rows["bbb"]["share_percent"] == 10.0
    assert result["total_requests"] == 1000.0


def test_after_promotion_there_is_no_canary_role():
    result = split.shares(response(aaa=50), stable="aaa", current="aaa")
    assert [row["role"] for row in result["replicasets"]] == ["stable"]


def test_no_traffic_or_failed_query_is_an_error_not_a_zero_percent_split():
    with pytest.raises(ValueError, match="No prediction requests"):
        split.shares({"status": "success", "data": {"result": []}}, "a", "b")
    with pytest.raises(ValueError, match="query failed"):
        split.shares({"status": "error"}, "a", "b")


def test_load_test_can_target_alb_but_only_validated_hostnames():
    script = (ROOT / "scripts/m33.sh").read_text()
    assert 'M33_TARGET:-service' in script
    assert "elb\\.amazonaws\\.com" in script
    assert "LOAD_BASE_URL" in script
    k6 = (ROOT / "deploy/monitoring/k6.js").read_text()
    assert "__ENV.LOAD_BASE_URL" in k6 and "inference-api.mlops.svc.cluster.local:8000" in k6
    template = (ROOT / "deploy/monitoring/k6-job.yaml.tpl").read_text()
    for key, value in {"LOAD_VUS": "10", "LOAD_RATE": "20", "LOAD_DURATION": "300s", "LOAD_PAYLOAD": "valid",
                       "LOAD_BASE_URL": "http://internal-x.ap-southeast-1.elb.amazonaws.com"}.items():
        template = template.replace("${" + key + "}", value)
    env = {item["name"]: item["value"] for item in yaml.safe_load(template)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LOAD_BASE_URL"].startswith("http://internal-")
    assert not re.search(r"\$\{\w+\}", template), "every placeholder must be substituted"


def test_dashboard_compares_replicasets_without_breaking_phase_three_panels():
    dashboard = json.loads((ROOT / "deploy/monitoring/inference-dashboard.json").read_text())
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    titles = [panel["title"] for panel in dashboard["panels"]]
    assert titles[:4] == ["Prediction request rate", "Prediction P95 latency",
                          "Prediction error rate", "Inference Pod scrape status (1 = UP)"]
    canary = [panel for panel in dashboard["panels"] if panel["title"].startswith("Canary:")]
    assert len(canary) == 3
    assert all("by (" in panel["targets"][0]["expr"] and "rollouts_pod_template_hash" in panel["targets"][0]["expr"]
               for panel in canary)


def test_status_and_split_scripts_stay_read_only():
    script = (ROOT / "scripts/m42.sh").read_text()
    for verb in ("apply", "delete", "patch", "create", "scale", "annotate", "label"):
        assert not re.search(rf"kubectl\b[^\n]*\b{verb}\b", script), verb
    assert "M42_WINDOW must look like" in script


def test_split_cleans_up_its_port_forward_without_unbound_local_variable():
    script = (ROOT / "scripts/m42.sh").read_text()
    # `local pid` is out of scope when an EXIT trap runs under `set -u`; expand the PID when the trap is set.
    assert "trap \"kill $pid 2>/dev/null || true\" EXIT" in script
    assert "trap 'kill" not in script


def test_load_env_names_never_collide_with_native_k6_options():
    """K6_VUS and K6_DURATION override `scenarios` entirely, silently disabling the arrival rate."""
    native = {"K6_VUS", "K6_DURATION", "K6_ITERATIONS", "K6_STAGES", "K6_MAX_VUS", "K6_SCENARIOS"}
    template = (ROOT / "deploy/monitoring/k6-job.yaml.tpl").read_text()
    script = (ROOT / "deploy/monitoring/k6.js").read_text()
    runner = (ROOT / "scripts/m33.sh").read_text()
    for text in (template, runner):
        assert not (native & set(re.findall(r"\bK6_[A-Z_]+\b", text))), "native k6 env name in use"
    for key in ("LOAD_VUS", "LOAD_RATE", "LOAD_DURATION", "LOAD_BASE_URL"):
        assert key in template and key in script and key in runner
    assert "constant-arrival-rate" in script


def test_invalid_payload_mode_is_opt_in_and_only_for_the_alb():
    script = (ROOT / "scripts/m33.sh").read_text()
    assert 'payload="${M33_PAYLOAD:-valid}"' in script
    assert "M33_PAYLOAD must be valid or invalid" in script
    assert 'Invalid payloads are only allowed against the ALB' in script
    assert "M33_PAYLOAD ?= valid" in (ROOT / "Makefile").read_text()
    k6 = (ROOT / "deploy/monitoring/k6.js").read_text()
    assert "LOAD_PAYLOAD || 'valid'" in k6
    assert "expectedStatuses(422)" in k6, "a 422 must not make k6 report the run as failed"


def test_the_invalid_payload_is_rejected_by_the_api_and_counted_as_an_error():
    """The release abort test relies on k6's invalid request becoming a 422 that errors_total records."""
    from inference.app import PredictionRequest
    from inference.metrics import Metrics
    from pydantic import ValidationError

    k6 = (ROOT / "deploy/monitoring/k6.js").read_text()
    assert "invalid ? 999 : 1" in k6
    with pytest.raises(ValidationError):
        PredictionRequest(pickup_location_id=999, dropoff_location_id=2, pickup_datetime="2025-04-07T16:00:00Z")
    metrics = Metrics()
    metrics.observe("POST", "/predict", 422, 0.01)
    rendered = metrics.render()
    assert 'inference_http_errors_total{method="POST",path="/predict",status="422"} 1' in rendered
    assert 'inference_http_requests_total{method="POST",path="/predict",status="422"} 1' in rendered
