"""PostSync verification followed by guarded MLflow champion promotion."""

from __future__ import annotations

import json
import os
import ssl
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import boto3
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

NAMESPACE = "mlops"
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


class KubeAPI:
    def __init__(self) -> None:
        self.base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.getenv('KUBERNETES_SERVICE_PORT', '443')}"
        self.token = open(TOKEN_PATH, encoding="utf-8").read().strip()
        self.context = ssl.create_default_context(cafile=CA_PATH)

    def call(self, method: str, path: str, body: dict | None = None) -> dict | None:
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, method=method,
                          headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urlopen(request, context=self.context, timeout=10) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as error:
            if error.code == 404 and method == "GET":
                return None
            raise


def acquire_lease(kube: KubeAPI, release_id: str) -> None:
    path = f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/inference-champion"
    now = datetime.now(timezone.utc)
    current = kube.call("GET", path)
    spec = {"holderIdentity": release_id, "leaseDurationSeconds": 120,
            "renewTime": now.isoformat().replace("+00:00", "Z")}
    if current is None:
        kube.call("POST", f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases",
                  {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                   "metadata": {"name": "inference-champion", "namespace": NAMESPACE}, "spec": spec})
        return
    old = current.get("spec", {})
    last = old.get("renewTime")
    age = (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() if last else 999999
    if old.get("holderIdentity") and age < old.get("leaseDurationSeconds", 120):
        raise RuntimeError(f"Promotion lease held by {old['holderIdentity']}")
    current["spec"] = spec
    kube.call("PUT", path, current)


def release_lease(kube: KubeAPI, release_id: str) -> None:
    path = f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/inference-champion"
    current = kube.call("GET", path)
    if current and current.get("spec", {}).get("holderIdentity") == release_id:
        current["spec"]["holderIdentity"] = ""
        kube.call("PUT", path, current)


def verify_workload(kube: KubeAPI, expected: dict) -> None:
    deployment = kube.call("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/inference-api")
    if deployment is None:
        raise ValueError("Serving Deployment missing")
    spec = deployment["spec"]
    status = deployment.get("status", {})
    if (spec["template"]["spec"]["containers"][0]["image"] != expected["image"] or
        spec["template"]["metadata"]["labels"].get("mlops.aws/model-version") != expected["model_version"] or
        spec["template"]["metadata"]["annotations"].get("mlops.aws/release-id") != expected["release_id"] or
        status.get("observedGeneration", 0) < deployment["metadata"]["generation"] or
        status.get("readyReplicas", 0) != spec["replicas"]):
        raise ValueError("Deployment is not the expected healthy release")
    pods = kube.call("GET", f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3Dinference-api")
    active = [item for item in pods["items"] if not item["metadata"].get("deletionTimestamp")]
    if len(active) != spec["replicas"]:
        raise ValueError("Unexpected active serving Pod count")
    digest = expected["image"].split("@", 1)[1]
    for pod in active:
        container = pod["spec"]["containers"][0]
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if (container["image"] != expected["image"] or not statuses or
            not statuses[0].get("ready") or not statuses[0].get("imageID", "").endswith(digest)):
            raise ValueError("Running Pod image digest or readiness differs")


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def verify_api(expected: dict) -> None:
    base = "http://inference-api.mlops.svc.cluster.local:8000"
    if _get_json(base + "/readyz").get("status") != "ready":
        raise ValueError("Inference API is not ready")
    info = _get_json(base + "/model-info")
    for field in ("model_name", "model_version", "artifact_checksum", "source_run_id", "serving_git_sha"):
        if info.get(field) != expected[field]:
            raise ValueError(f"Serving metadata differs: {field}")
    if info.get("git_sha") != expected["training_git_sha"] or info.get("approval_status") != "approved":
        raise ValueError("Serving training provenance differs")
    request = Request(base + "/predict", method="POST", headers={"Content-Type": "application/json"},
                      data=b'{"pickup_location_id":1,"dropoff_location_id":2,"pickup_datetime":"2025-04-07T16:00:00Z"}')
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    if (result.get("model_name"), result.get("model_version")) != (expected["model_name"], expected["model_version"]):
        raise ValueError("Prediction response refers to another model")
    prediction = result.get("predicted_duration_minutes")
    if not isinstance(prediction, (float, int)) or not 0 < prediction < 1440:
        raise ValueError("Prediction smoke test failed")


def verify_registry(client: MlflowClient, expected: dict) -> None:
    registered = client.get_model_version(expected["model_name"], expected["model_version"])
    if (registered.status != "READY" or registered.tags.get("validation_status") != "approved" or
        registered.run_id != expected["mlflow_run_id"] or
        registered.tags.get("source_run_id") != expected["source_run_id"] or
        registered.tags.get("git_sha") != expected["training_git_sha"]):
        raise ValueError("Registry approval or provenance changed")


def promote(kube: KubeAPI, client: MlflowClient, expected: dict) -> str:
    acquire_lease(kube, expected["release_id"])
    try:
        verify_workload(kube, expected)
        verify_api(expected)
        verify_registry(client, expected)
        # Check the deployment again while holding the promotion lease.
        verify_workload(kube, expected)
        try:
            current = client.get_model_version_by_alias(expected["model_name"], "champion")
        except MlflowException as error:
            if error.error_code != "INVALID_PARAMETER_VALUE" or "alias champion not found" not in str(error).lower():
                raise
            current = None
        if current and current.version == expected["model_version"]:
            print(f"champion already points to version {current.version}; release {expected['release_id']}")
            return "already-promoted"
        client.set_registered_model_alias(expected["model_name"], "champion", expected["model_version"])
        print(f"champion promoted to version {expected['model_version']}; release {expected['release_id']}")
        return "promoted"
    finally:
        release_lease(kube, expected["release_id"])


def main() -> None:
    fields = ("release_id", "model_name", "model_version", "image", "artifact_checksum", "mlflow_run_id",
              "source_run_id", "training_git_sha", "serving_git_sha")
    expected = {field: os.environ[f"M32_{field.upper()}"] for field in fields}
    if not expected["image"].startswith("160616607747.dkr.ecr.ap-southeast-1.amazonaws.com/mlops-aws-inference-dev@sha256:"):
        raise ValueError("Expected image must be a pinned inference ECR digest")
    client = MlflowClient(tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow.mlops.svc.cluster.local:5000"))
    bucket = os.environ["M32_BUCKET"]
    prefix = os.environ["M32_RELEASE_PREFIX"]
    s3 = boto3.client("s3")
    try:
        result = promote(KubeAPI(), client, expected)
    except Exception as error:
        s3.put_object(Bucket=bucket, Key=f"{prefix}/failed.json",
                      Body=json.dumps({"release_id": expected["release_id"], "reason": str(error),
                                       "time": datetime.now(timezone.utc).isoformat()}, sort_keys=True).encode(),
                      ContentType="application/json")
        raise
    s3.put_object(Bucket=bucket, Key=f"{prefix}/verified.json",
                  Body=json.dumps({"release_id": expected["release_id"], "result": result,
                                   "image": expected["image"], "model_version": expected["model_version"],
                                   "time": datetime.now(timezone.utc).isoformat()}, sort_keys=True).encode(),
                  ContentType="application/json")


if __name__ == "__main__":
    main()
