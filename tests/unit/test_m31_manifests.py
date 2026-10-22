from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/gitops/inference"


def test_inference_deployment_is_private_digest_pinned_and_scheduled():
    deployment = yaml.safe_load((DEPLOY / "deployment.yaml").read_text())
    service = yaml.safe_load((DEPLOY / "service.yaml").read_text())
    pod = deployment["spec"]["template"]
    container = pod["spec"]["containers"][0]
    assert deployment["metadata"]["name"] == service["metadata"]["name"] == "inference-api"
    assert deployment["metadata"]["namespace"] == service["metadata"]["namespace"] == "mlops"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    assert container["image"].startswith("160616607747.dkr.ecr.ap-southeast-1.amazonaws.com/mlops-aws-inference-dev@sha256:")
    assert len(container["image"].split("@sha256:")[1]) == 64
    version = deployment["metadata"]["labels"]["mlops.aws/model-version"]
    assert version.isdecimal() and int(version) > 0
    assert pod["metadata"]["labels"]["mlops.aws/model-version"] == version
    assert pod["spec"]["nodeSelector"] == {"mlops.aws/workload": "inference", "karpenter.sh/capacity-type": "on-demand"}
    assert pod["spec"]["tolerations"] == [{"key": "mlops.aws/workload", "operator": "Equal", "value": "inference", "effect": "NoSchedule"}]
    assert container["startupProbe"]["httpGet"]["path"] == "/readyz"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert pod["spec"]["terminationGracePeriodSeconds"] >= 30
    assert container["resources"]["requests"]["cpu"] and container["resources"]["limits"]["memory"]
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 8000
    assert {"deployment.yaml", "kustomization.yaml", "service.yaml"}.issubset(
        {path.name for path in DEPLOY.iterdir()})


def test_cleanup_scope_is_only_serving_objects():
    script = (ROOT / "scripts/m31.sh").read_text()
    assert "kubectl -n mlops delete deployment/inference-api service/inference-api --ignore-not-found" in script
