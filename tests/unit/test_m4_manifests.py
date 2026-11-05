"""Guard the Phase 4 canary design: workloadRef adoption, ALB weights, metric gates."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GITOPS = ROOT / "deploy/gitops/inference"


def load_all(name: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all((GITOPS / name).read_text()) if doc]


def rollout() -> dict:
    return load_all("rollout.yaml")[0]


def test_rollout_adopts_the_release_deployment_without_replacing_it():
    doc = rollout()
    assert (doc["kind"], doc["metadata"]["name"], doc["metadata"]["namespace"]) == ("Rollout", "inference-api", "mlops")
    ref = doc["spec"]["workloadRef"]
    assert ref == {"apiVersion": "apps/v1", "kind": "Deployment", "name": "inference-api", "scaleDown": "onsuccess"}
    # Release tooling still edits only the Deployment template.
    assert "template" not in doc["spec"]
    deployment = yaml.safe_load((GITOPS / "deployment.yaml").read_text())
    assert deployment["metadata"]["name"] == ref["name"]


def test_canary_steps_hold_traffic_at_zero_then_ten_then_fifty():
    canary = rollout()["spec"]["strategy"]["canary"]
    assert canary["stableService"] == "inference-api"
    assert canary["canaryService"] == "inference-api-canary"
    assert canary["trafficRouting"]["alb"] == {"ingress": "inference-api", "servicePort": 8000}
    weights = [step["setWeight"] for step in canary["steps"] if "setWeight" in step]
    assert weights == [0, 10, 50]
    assert canary["steps"][0] == {"setCanaryScale": {"replicas": 1}}
    smoke_index = next(i for i, step in enumerate(canary["steps"]) if "analysis" in step)
    first_traffic = next(i for i, step in enumerate(canary["steps"]) if step.get("setWeight", 0) > 0)
    assert smoke_index < first_traffic, "smoke test must run before real traffic"
    assert canary["analysis"]["startingStep"] == first_traffic
    assert canary["analysis"]["templates"] == [{"templateName": "inference-canary-gate"}]
    args = {item["name"]: item["valueFrom"]["podTemplateHashValue"] for item in canary["analysis"]["args"]}
    assert args == {"canary-hash": "Latest", "stable-hash": "Stable"}


def test_services_and_internal_alb():
    stable, canary = yaml.safe_load((GITOPS / "service.yaml").read_text()), yaml.safe_load((GITOPS / "service-canary.yaml").read_text())
    assert stable["metadata"]["name"] == "inference-api" and canary["metadata"]["name"] == "inference-api-canary"
    assert stable["spec"]["type"] == canary["spec"]["type"] == "ClusterIP"
    ingress = yaml.safe_load((GITOPS / "ingress.yaml").read_text())
    annotations = ingress["metadata"]["annotations"]
    assert annotations["alb.ingress.kubernetes.io/scheme"] == "internal"
    assert annotations["alb.ingress.kubernetes.io/target-type"] == "ip"
    assert annotations["alb.ingress.kubernetes.io/inbound-cidrs"] == "10.20.0.0/16"
    assert not any(key.startswith("alb.ingress.kubernetes.io/actions.") for key in annotations), \
        "Argo Rollouts owns the weighted action annotation"
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "inference-api", "port": {"name": "use-annotation"}}


def test_analysis_gates_cover_errors_latency_and_traffic_and_use_the_rollout_hash():
    gate = load_all("analysis-template.yaml")[0]
    assert gate["kind"] == "AnalysisTemplate" and gate["metadata"]["name"] == "inference-canary-gate"
    metrics = {item["name"]: item for item in gate["spec"]["metrics"]}
    assert set(metrics) == {"error-rate", "p95-latency-seconds", "p95-latency-vs-stable", "canary-request-rate"}
    for name, metric in metrics.items():
        query = metric["provider"]["prometheus"]["query"]
        assert metric["provider"]["prometheus"]["address"] == "http://m33-prometheus-server.monitoring.svc.cluster.local:80"
        assert "{{args.canary-hash}}" in query, name
        assert "vector(0) / vector(0)" in query, f"{name}: no samples must be inconclusive, not success"
        assert metric["successCondition"] and metric["failureCondition"], name
        assert "count" not in metric, "background analysis runs until the rollout ends"
    assert "{{args.stable-hash}}" in metrics["p95-latency-vs-stable"]["provider"]["prometheus"]["query"]
    assert "rate(inference_http_errors_total" in metrics["error-rate"]["provider"]["prometheus"]["query"]
    assert metrics["error-rate"]["successCondition"] == "result[0] < 0.01"
    assert metrics["p95-latency-seconds"]["successCondition"] == "result[0] < 0.5"


def test_smoke_analysis_targets_only_the_canary_service():
    smoke = load_all("analysis-template.yaml")[1]
    assert smoke["metadata"]["name"] == "inference-canary-smoke"
    script = smoke["spec"]["metrics"][0]["provider"]["job"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "inference-api-canary" in script
    assert "http://inference-api." not in script and "inference-api:8000" not in script


def test_kustomization_and_prometheus_wire_everything_together():
    kustomization = yaml.safe_load((GITOPS / "kustomization.yaml").read_text())
    assert {"rollout.yaml", "service-canary.yaml", "ingress.yaml", "analysis-template.yaml"} <= set(kustomization["resources"])
    prom = yaml.safe_load((ROOT / "deploy/monitoring/prometheus-values.yaml").read_text())
    relabels = prom["serverFiles"]["prometheus.yml"]["scrape_configs"][0]["relabel_configs"]
    assert {"source_labels": ["__meta_kubernetes_pod_label_rollouts_pod_template_hash"],
            "target_label": "rollouts_pod_template_hash"} in relabels
