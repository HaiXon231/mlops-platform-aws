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

EXIT_CANARY_ABORTED = 3


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


def template_matches(deployment: dict, expected: dict) -> bool:
    """The Deployment is the release template (Rollout adopts it through workloadRef)."""
    template = deployment["spec"]["template"]
    return (template["spec"]["containers"][0]["image"] == expected["image"] and
            template["metadata"]["annotations"].get("mlops.aws/release-id") == expected["release_id"] and
            template["metadata"]["annotations"].get("mlops.aws/artifact-checksum") == expected["artifact_checksum"] and
            template["metadata"]["labels"].get("mlops.aws/model-version") == expected["model_version"])


def rollout_state(deployment: dict, rollout: dict, expected: dict) -> str:
    """Classify the live Rollout for this release: promoted, aborted or progressing.

    "promoted" means the new ReplicaSet is the stable one and nothing else is rolling.
    "aborted" means Argo Rollouts rejected this exact template; the stable ReplicaSet still
    serves the previous release.
    """
    if not template_matches(deployment, expected):
        return "progressing"
    status = rollout.get("status", {})
    # With workloadRef, the controller records which Deployment generation it has seen.
    if str(status.get("workloadObservedGeneration", "")) != str(deployment["metadata"]["generation"]):
        return "progressing"
    current, stable = status.get("currentPodHash"), status.get("stableRS")
    if status.get("abort") is True or status.get("phase") == "Degraded":
        return "aborted" if current and current != stable else "progressing"
    if status.get("phase") == "Healthy" and current and current == stable:
        return "promoted"
    return "progressing"


def promoted_pods_ready(rollout: dict, expected: dict) -> bool:
    """Every live Pod of the stable ReplicaSet runs the released digest and is Ready."""
    stable = rollout["status"]["stableRS"]
    pods = command_json("-n", "mlops", "get", "pods", "-l",
                        f"app=inference-api,rollouts-pod-template-hash={stable}")["items"]
    active = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    digest = expected["image"].split("@", 1)[1]
    return bool(active) and all(
        pod["spec"]["containers"][0]["image"] == expected["image"] and
        pod.get("status", {}).get("containerStatuses") and
        pod["status"]["containerStatuses"][0].get("ready") and
        pod["status"]["containerStatuses"][0].get("imageID", "").endswith(digest)
        for pod in active)


def live_state(expected: dict) -> str:
    deployment = command_json("-n", "mlops", "get", "deployment", "inference-api")
    rollout = command_json("-n", "mlops", "get", "rollout.argoproj.io", "inference-api")
    state = rollout_state(deployment, rollout, expected)
    if state == "promoted" and not promoted_pods_ready(rollout, expected):
        return "progressing"
    return state


def workload_matches(expected: dict) -> bool:
    return live_state(expected) == "promoted"


def wait_for_outcome(expected: dict, commit: str, timeout: int = 1800) -> str:
    """Block until the canary is promoted ("promoted") or rejected ("aborted")."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if current_main() != commit:
            raise RuntimeError("main moved to another release before deployment completed")
        try:
            state = live_state(expected)
            if state == "aborted":
                return "aborted"
            if state == "promoted" and application_matches(commit):
                return "promoted"
        except (subprocess.CalledProcessError, KeyError, ValueError) as error:
            last_error = str(error)
        time.sleep(10)
    raise TimeoutError(f"Canary did not finish promotion or abort in time: {last_error}")


def wait_for_reverted(commit: str, timeout: int = 900) -> None:
    """After a revert commit, Argo CD must be Synced/Healthy at it and the Rollout stable again."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            rollout = command_json("-n", "mlops", "get", "rollout.argoproj.io", "inference-api")
            status = rollout.get("status", {})
            if (application_matches(commit) and status.get("phase") == "Healthy" and
                    status.get("currentPodHash") == status.get("stableRS")):
                return
        except (subprocess.CalledProcessError, KeyError, ValueError) as error:
            last_error = str(error)
        time.sleep(10)
    raise TimeoutError(f"Rollback commit did not return the Rollout to stable: {last_error}")


def wait_for_release(expected: dict, commit: str, timeout: int = 900) -> None:
    """Wait until this release is fully promoted (kept for the verify-after-promotion path)."""
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
    raise TimeoutError(f"Argo CD or serving Rollout did not reach this release: {last_error}")


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
    parser.add_argument("--wait-only", action="store_true",
                        help="Wait for Argo and the new Pod before opening an inference port-forward")
    parser.add_argument("--wait-outcome", action="store_true",
                        help="Wait for the canary to be promoted (exit 0) or aborted (exit 3)")
    parser.add_argument("--wait-reverted", action="store_true",
                        help="Wait until --commit (a revert) is live and the Rollout is stable again")
    args = parser.parse_args()
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    if args.wait_reverted:
        wait_for_reverted(args.commit)
        print(json.dumps({"gitops_commit": args.commit, "rollback": "stable-restored"}, sort_keys=True))
        return
    if args.wait_outcome:
        outcome = wait_for_outcome(expected, args.commit)
        print(json.dumps({"gitops_commit": args.commit, "image": expected["image"],
                          "canary": outcome}, sort_keys=True))
        if outcome == "aborted":
            raise SystemExit(EXIT_CANARY_ABORTED)
        return
    wait_for_release(expected, args.commit)
    if args.wait_only:
        print(json.dumps({"gitops_commit": args.commit, "image": expected["image"],
                          "deployment": "ready"}, sort_keys=True))
        return
    duration = verify_api(expected, args.inference_uri)
    client = MlflowClient(tracking_uri=args.mlflow_uri)
    verify_registry(client, expected)
    result = promote(client, expected, args.commit)
    print(json.dumps({"release_id": expected["release_id"], "gitops_commit": args.commit,
                      "image": expected["image"], "model_version": expected["model_version"],
                      "prediction_minutes": duration, "champion": result}, sort_keys=True))


if __name__ == "__main__":
    main()
