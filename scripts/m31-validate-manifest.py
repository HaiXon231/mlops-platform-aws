"""Fail closed until an immutable ECR digest is pinned for the approved version."""

import re
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1]
deployment = yaml.safe_load((root / "deploy/gitops/inference/deployment.yaml").read_text())
service = yaml.safe_load((root / "deploy/gitops/inference/service.yaml").read_text())
pod = deployment["spec"]["template"]
container = pod["spec"]["containers"][0]
image = container["image"]
assert re.fullmatch(r"\d+\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/mlops-aws-inference-dev@sha256:[0-9a-f]{64}", image), "Use existing inference ECR repository and a digest"
assert not image.endswith("0" * 64), "Replace placeholder digest before deployment"
assert deployment["metadata"]["namespace"] == service["metadata"]["namespace"] == "mlops"
version = deployment["metadata"]["labels"]["mlops.aws/model-version"]
assert version.isdecimal() and int(version) > 0
assert pod["metadata"]["labels"]["mlops.aws/model-version"] == version
assert service["spec"]["type"] == "ClusterIP"
assert pod["spec"]["nodeSelector"]["mlops.aws/workload"] == "inference"
assert pod["spec"]["nodeSelector"]["karpenter.sh/capacity-type"] == "on-demand"
assert any(item == {"key": "mlops.aws/workload", "operator": "Equal", "value": "inference", "effect": "NoSchedule"} for item in pod["spec"]["tolerations"])
assert container["resources"]["requests"]["memory"] and container["resources"]["limits"]["memory"]
print("Inference manifest validated for pinned digest, model version, and scheduling")
