"""Read-only comparison of manually deployed serving resources with GitOps state."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "deploy/gitops/inference"


def _live(kind: str, name: str) -> dict:
    result = subprocess.run(["kubectl", "-n", "mlops", "get", kind, name, "-o", "json"],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _subset(expected, actual, path="root") -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise ValueError(f"{path} differs")
        for key, value in expected.items():
            _subset(value, actual.get(key), f"{path}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise ValueError(f"{path} differs")
        for index, value in enumerate(expected):
            _subset(value, actual[index], f"{path}[{index}]")
    elif expected != actual:
        raise ValueError(f"{path}: desired {expected!r}, live {actual!r}")


def check() -> None:
    deployment = yaml.safe_load((DIRECTORY / "deployment.yaml").read_text())
    service = yaml.safe_load((DIRECTORY / "service.yaml").read_text())
    actual_deployment = _live("deployment", "inference-api")
    actual_service = _live("service", "inference-api")
    expected_spec = deployment["spec"]
    live_spec = actual_deployment["spec"]
    # HPA owns live replicas after M3.3; the GitOps template still pins one as
    # its initial value for adoption and recovery.
    for field in ("revisionHistoryLimit", "strategy", "selector"):
        _subset(expected_spec[field], live_spec.get(field), "deployment.spec." + field)
    desired_pod = expected_spec["template"]
    live_pod = live_spec["template"]
    for field in ("labels", "annotations"):
        _subset(desired_pod["metadata"].get(field, {}), live_pod["metadata"].get(field, {}), "pod.metadata." + field)
    for field in ("terminationGracePeriodSeconds", "nodeSelector", "tolerations", "securityContext", "containers"):
        _subset(desired_pod["spec"][field], live_pod["spec"].get(field), "pod.spec." + field)
    for field in ("type", "selector", "ports"):
        _subset(service["spec"][field], actual_service["spec"].get(field), "service.spec." + field)
    replicas = live_spec.get("replicas", 0)
    if replicas < 1 or actual_deployment.get("status", {}).get("readyReplicas", 0) != replicas:
        raise ValueError("Serving deployment is not Ready")
    print("Live inference Deployment/Service match desired GitOps state and are Ready")


if __name__ == "__main__":
    check()
