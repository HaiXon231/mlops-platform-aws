"""Render the export request and serving release without reaching the cluster."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

from inference.release_s3 import release_prefix

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "deploy/gitops/export"
INFERENCE = ROOT / "deploy/gitops/inference"
ECR = "160616607747.dkr.ecr.ap-southeast-1.amazonaws.com/mlops-aws-inference-dev"


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _pin(image: str) -> None:
    if not re.fullmatch(re.escape(ECR) + r"@sha256:[0-9a-f]{64}", image):
        raise ValueError("Image must be the inference ECR repository pinned by digest")


def _set_resource(directory: Path, name: str) -> None:
    path = directory / "kustomization.yaml"
    doc = _read(path)
    if name not in doc["resources"]:
        doc["resources"].append(name)
    _write(path, doc)


def stage(name: str, version: str, release_id: str, serving_git_sha: str,
          control_image: str, bucket: str) -> dict:
    prefix = release_prefix(name, version, release_id)
    _pin(control_image)
    if not re.fullmatch(r"[0-9a-f]{40}", serving_git_sha):
        raise ValueError("Serving Git SHA must be a full commit hash")
    if not re.fullmatch(r"mlops-aws-model-artifacts-dev-\d{12}-ap-southeast-1", bucket):
        raise ValueError("Unexpected model artifacts bucket")
    doc = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "inference-release-export", "namespace": "mlops",
                     "annotations": {"argocd.argoproj.io/hook": "Sync",
                                     "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
                                     "mlops.aws/release-id": release_id}},
        "spec": {"backoffLimit": 1, "activeDeadlineSeconds": 1800,
                 "template": {"spec": {"restartPolicy": "Never", "serviceAccountName": "inference-release-export",
                                       "containers": [{"name": "export", "image": control_image,
                                                       "imagePullPolicy": "IfNotPresent",
                                                       "command": ["python", "-m", "inference.release_s3"],
                                                       "args": ["export", "--bucket", bucket,
                                                                "--model-name", name, "--model-version", version,
                                                                "--release-id", release_id,
                                                                "--serving-git-sha", serving_git_sha],
                                                       "env": [{"name": "AWS_REGION", "value": "ap-southeast-1"}],
                                                       "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                                                     "limits": {"cpu": "1", "memory": "2Gi"}}}]}}}
    }
    path = EXPORT / "job.yaml"
    if path.exists():
        old = _read(path)
        if old["metadata"]["annotations"].get("mlops.aws/release-id") == release_id:
            if old != doc:
                raise ValueError("Release ID already staged with different inputs")
            return doc
    _write(path, doc)
    _set_resource(EXPORT, "job.yaml")
    return doc


def finalize(completion: dict, image: str, control_image: str, bucket: str) -> dict:
    _pin(image)
    _pin(control_image)
    name, version, release_id = (completion[key] for key in ("model_name", "model_version", "release_id"))
    prefix = release_prefix(name, version, release_id)
    staged = _read(EXPORT / "job.yaml")
    args = staged["spec"]["template"]["spec"]["containers"][0]["args"]
    staged_values = dict(zip(args[1::2], args[2::2]))
    if (staged["metadata"]["annotations"].get("mlops.aws/release-id") != release_id or
        staged["spec"]["template"]["spec"]["containers"][0]["image"] != control_image or
        staged_values.get("--model-name") != name or staged_values.get("--model-version") != version or
        staged_values.get("--serving-git-sha") != completion["serving_git_sha"] or
        staged_values.get("--bucket") != bucket):
        raise ValueError("Export request and completion differ")
    deployment_path = INFERENCE / "deployment.yaml"
    deployment = _read(deployment_path)
    template = deployment["spec"]["template"]
    deployment["metadata"]["labels"]["mlops.aws/model-version"] = version
    template["metadata"]["labels"]["mlops.aws/model-version"] = version
    template["metadata"].setdefault("annotations", {})["mlops.aws/release-id"] = release_id
    template["spec"]["containers"][0]["image"] = image
    _write(deployment_path, deployment)
    expected = {"release_id": release_id, "model_name": name, "model_version": version,
                "image": image, "artifact_checksum": completion["artifact_checksum"],
                "mlflow_run_id": completion["mlflow_run_id"], "source_run_id": completion["source_run_id"],
                "training_git_sha": completion["training_git_sha"],
                "serving_git_sha": completion["serving_git_sha"]}
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "inference-release-verify", "namespace": "mlops",
                     "annotations": {"argocd.argoproj.io/hook": "PostSync",
                                     "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
                                     "mlops.aws/release-id": release_id}},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 600,
                 "template": {"spec": {"restartPolicy": "Never", "serviceAccountName": "inference-release-verify",
                                       "containers": [{"name": "verify", "image": control_image,
                                                       "imagePullPolicy": "IfNotPresent",
                                                       "command": ["python", "-m", "inference.promote"],
                                                       "env": [{"name": "M32_" + key.upper(), "value": value}
                                                               for key, value in expected.items()] + [
                                                           {"name": "M32_BUCKET", "value": bucket},
                                                           {"name": "M32_RELEASE_PREFIX", "value": prefix},
                                                           {"name": "AWS_REGION", "value": "ap-southeast-1"}],
                                                       "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                                                     "limits": {"cpu": "500m", "memory": "1Gi"}}}]}}}
    }
    _write(INFERENCE / "post-sync-verification.yaml", job)
    _set_resource(INFERENCE, "post-sync-verification.yaml")
    return expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    first = sub.add_parser("stage")
    for key in ("model-name", "model-version", "release-id", "serving-git-sha", "control-image", "bucket"):
        first.add_argument("--" + key, required=True)
    second = sub.add_parser("finalize")
    second.add_argument("--completion", type=Path, required=True)
    for key in ("image", "control-image", "bucket"):
        second.add_argument("--" + key, required=True)
    args = parser.parse_args()
    if args.mode == "stage":
        result = stage(args.model_name, args.model_version, args.release_id,
                       args.serving_git_sha, args.control_image, args.bucket)
    else:
        result = finalize(json.loads(args.completion.read_text()), args.image, args.control_image, args.bucket)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
