"""Verify one Argo CD serving release, then promote its approved Registry version."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

from mlflow import MlflowClient
from mlflow.exceptions import MlflowException


def command_json(*arguments: str) -> dict:
    result = subprocess.run(["kubectl", "--request-timeout=15s", *arguments, "-o", "json"],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def current_main() -> str:
    result = subprocess.run(["git", "ls-remote", "origin", "refs/heads/main"],
                            check=True, capture_output=True, text=True)
    return result.stdout.split()[0]


def application_matches(commit: str) -> bool:
    app = command_json("-n", "argocd", "get", "application", "inference-api")
    return (app["spec"]["source"]["targetRevision"] == "main" and
            app.get("status", {}).get("sync", {}).get("revision") == commit and
            app.get("status", {}).get("sync", {}).get("status") == "Synced" and
            app.get("status", {}).get("health", {}).get("status") == "Healthy")


def workload_matches(expected: dict) -> bool:
    deployment = command_json("-n", "mlops", "get", "deployment", "inference-api")
    template = deployment["spec"]["template"]
    spec = template["spec"]
    status = deployment.get("status", {})
    replicas = deployment["spec"]["replicas"]
    if (spec["containers"][0]["image"] != expected["image"] or
        template["metadata"]["annotations"].get("mlops.aws/release-id") != expected["release_id"] or
        template["metadata"]["annotations"].get("mlops.aws/artifact-checksum") != expected["artifact_checksum"] or
        template["metadata"]["labels"].get("mlops.aws/model-version") != expected["model_version"] or
        status.get("observedGeneration", 0) < deployment["metadata"]["generation"] or
        status.get("availableReplicas", 0) != replicas or status.get("readyReplicas", 0) != replicas):
        return False
    pods = command_json("-n", "mlops", "get", "pods", "-l", "app=inference-api")["items"]
    active = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    digest = expected["image"].split("@", 1)[1]
    return (len(active) == replicas and all(
        pod["spec"]["containers"][0]["image"] == expected["image"] and
        pod.get("status", {}).get("containerStatuses") and
        pod["status"]["containerStatuses"][0].get("ready") and
        pod["status"]["containerStatuses"][0].get("imageID", "").endswith(digest)
        for pod in active))


def wait_for_release(expected: dict, commit: str, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if current_main() != commit:
            raise RuntimeError("main moved to another release before deployment completed")
        try:
            if application_matches(commit) and workload_matches(expected):
                return
        except (subprocess.CalledProcessError, KeyError, ValueError) as error:
            last_error = str(error)
        time.sleep(10)
    raise TimeoutError(f"Argo CD or serving Deployment did not reach this release: {last_error}")


def get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def verify_api(expected: dict, base_url: str) -> float:
    if get_json(base_url + "/readyz").get("status") != "ready":
        raise ValueError("Inference readiness failed")
    info = get_json(base_url + "/model-info")
    for key in ("model_name", "model_version", "release_id", "artifact_checksum",
                "mlflow_run_id", "source_run_id", "serving_git_sha"):
        if info.get(key) != expected[key]:
            raise ValueError(f"Serving model metadata differs: {key}")
    if info.get("git_sha") != expected["git_sha"] or info.get("approval_status") != "approved":
        raise ValueError("Serving training provenance or approval differs")
    request = Request(base_url + "/predict", method="POST", headers={"Content-Type": "application/json"},
                      data=b'{"pickup_location_id":1,"dropoff_location_id":2,"pickup_datetime":"2025-04-07T16:00:00Z"}')
    with urlopen(request, timeout=10) as response:
        prediction = json.load(response)
    duration = prediction.get("predicted_duration_minutes")
    if (prediction.get("model_name"), prediction.get("model_version")) != (
            expected["model_name"], expected["model_version"]):
        raise ValueError("Prediction used another model version")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration < 1440:
        raise ValueError("Prediction smoke test failed")
    return float(duration)


def verify_registry(client: MlflowClient, expected: dict) -> None:
    registered = client.get_model_version(expected["model_name"], expected["model_version"])
    tags = registered.tags or {}
    if (registered.status != "READY" or tags.get("validation_status") != "approved" or
        registered.run_id != expected["mlflow_run_id"] or
        tags.get("source_run_id") != expected["source_run_id"] or
        tags.get("git_sha") != expected["git_sha"]):
        raise ValueError("Cloud Registry approval or provenance differs from release")


def assert_current_release(expected: dict, commit: str) -> None:
    if current_main() != commit or not application_matches(commit) or not workload_matches(expected):
        raise RuntimeError("Release is no longer the desired, healthy GitOps revision")


def promote(client: MlflowClient, expected: dict, commit: str) -> str:
    assert_current_release(expected, commit)
    verify_registry(client, expected)
    try:
        current = client.get_model_version_by_alias(expected["model_name"], "champion")
    except MlflowException as error:
        if error.error_code not in {"INVALID_PARAMETER_VALUE", "RESOURCE_DOES_NOT_EXIST"} or "alias" not in str(error).lower():
            raise
        current = None
    if current is not None and current.version == expected["model_version"] and current.run_id == expected["mlflow_run_id"]:
        result = "already-promoted"
    else:
        assert_current_release(expected, commit)
        client.set_registered_model_alias(expected["model_name"], "champion", expected["model_version"])
        result = "promoted"
    confirmed = client.get_model_version_by_alias(expected["model_name"], "champion")
    if confirmed.version != expected["model_version"] or confirmed.run_id != expected["mlflow_run_id"]:
        raise RuntimeError("Champion alias read-back differs from verified release")
    assert_current_release(expected, commit)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--mlflow-uri", required=True)
    parser.add_argument("--inference-uri", required=True)
    args = parser.parse_args()
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    wait_for_release(expected, args.commit)
    duration = verify_api(expected, args.inference_uri)
    client = MlflowClient(tracking_uri=args.mlflow_uri)
    verify_registry(client, expected)
    result = promote(client, expected, args.commit)
    print(json.dumps({"release_id": expected["release_id"], "gitops_commit": args.commit,
                      "image": expected["image"], "model_version": expected["model_version"],
                      "prediction_minutes": duration, "champion": result}, sort_keys=True))


if __name__ == "__main__":
    main()
