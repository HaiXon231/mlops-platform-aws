"""Read-only state collectors for GitHub, Kubernetes, Argo, MLflow, Prometheus and AWS.

Each probe returns plain dicts so the verification engine can be tested on fixtures.
Nothing here mutates cloud state.
"""
from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Iterator

import yaml

from demo.runner.spec import ROOT

K = ["kubectl", "--request-timeout=20s"]


def kube_json(sh, *args: str) -> dict | None:
    result = sh.run([*K, *args, "-o", "json"], timeout=40)
    return result.json() if result.ok else None


def git(sh, *args: str) -> str | None:
    result = sh.run(["git", *args], timeout=60)
    return result.stdout.strip() if result.ok else None


def origin_main(sh) -> str | None:
    """Fetch then return the remote main SHA (read-only for the remote)."""
    sh.run(["git", "fetch", "--quiet", "origin", "main"], timeout=60)
    return git(sh, "rev-parse", "origin/main")


def git_yaml(sh, ref: str, path: str) -> dict | None:
    text = git(sh, "show", f"{ref}:{path}")
    return yaml.safe_load(text) if text else None


def gitops_template(sh, ref: str = "origin/main") -> dict | None:
    deployment = git_yaml(sh, ref, "deploy/gitops/inference/deployment.yaml")
    if not deployment:
        return None
    template = deployment["spec"]["template"]
    container = template["spec"]["containers"][0]
    return {"image": container["image"], "model_version": template["metadata"]["labels"].get("mlops.aws/model-version"),
            "release_id": template["metadata"].get("annotations", {}).get("mlops.aws/release-id"),
            "cpu_limit": str(container.get("resources", {}).get("limits", {}).get("cpu"))}


def serving_state(sh) -> dict:
    """Rollout, Argo CD Application and live Deployment template in one snapshot."""
    rollout = kube_json(sh, "-n", "mlops", "get", "rollout.argoproj.io", "inference-api") or {}
    app = kube_json(sh, "-n", "argocd", "get", "application", "inference-api") or {}
    deployment = kube_json(sh, "-n", "mlops", "get", "deployment", "inference-api") or {}
    status = rollout.get("status", {})
    template = deployment.get("spec", {}).get("template", {})
    containers = template.get("spec", {}).get("containers") or [{}]
    return {
        "rollout_found": bool(rollout),
        "phase": status.get("phase"), "abort": bool(status.get("abort")),
        "stable_hash": status.get("stableRS"), "current_hash": status.get("currentPodHash"),
        "paused": bool(status.get("pauseConditions")) or bool(rollout.get("spec", {}).get("paused")),
        "step_index": status.get("currentStepIndex"),
        "app_sync": app.get("status", {}).get("sync", {}).get("status"),
        "app_revision": app.get("status", {}).get("sync", {}).get("revision"),
        "app_health": app.get("status", {}).get("health", {}).get("status"),
        "live_image": containers[0].get("image"),
        "live_release_id": template.get("metadata", {}).get("annotations", {}).get("mlops.aws/release-id"),
    }


def analysis_runs(sh) -> list[dict]:
    doc = kube_json(sh, "-n", "mlops", "get", "analysisrun.argoproj.io") or {"items": []}
    runs = []
    for item in doc["items"]:
        metrics = {}
        for metric in item.get("status", {}).get("metricResults", []):
            values = [m.get("value") for m in metric.get("measurements", []) if m.get("value") is not None]
            metrics[metric["name"]] = {"phase": metric.get("phase"), "last_values": values[-3:]}
        runs.append({"name": item["metadata"]["name"], "created": item["metadata"].get("creationTimestamp"),
                     "phase": item.get("status", {}).get("phase"),
                     "message": item.get("status", {}).get("message"),
                     "canary_hash": item["metadata"].get("labels", {}).get("rollouts-pod-template-hash"),
                     "metrics": metrics})
    return sorted(runs, key=lambda run: run["created"] or "")


def active_jobs(sh) -> dict[str, list[str]]:
    """Project Jobs that would conflict with another scenario."""
    found = {}
    for key, args in {"training": ("-n", "mlops", "get", "jobs"),
                      "k6": ("-n", "mlops", "get", "jobs", "-l", "app=m33-k6"),
                      "release_traffic": ("-n", "mlops-release-traffic", "get", "jobs")}.items():
        doc = kube_json(sh, *args) or {"items": []}
        names = [job["metadata"]["name"] for job in doc["items"] if job.get("status", {}).get("active")]
        if key == "training":
            names = [name for name in names if name.startswith("mlops-training-")]
        found[key] = names
    return found


def gh_json(sh, *args: str) -> Any:
    result = sh.run(["gh", *args], timeout=60)
    return result.json() if result.ok and result.stdout.strip() else None


RUN_FIELDS = "databaseId,status,conclusion,createdAt,updatedAt,headSha,event,url,workflowName,displayTitle"


def workflow_runs(sh, repo: str, workflow: str, limit: int = 10) -> list[dict]:
    return gh_json(sh, "run", "list", "--repo", repo, "--workflow", workflow, "--limit", str(limit),
                   "--json", RUN_FIELDS) or []


def active_runs(sh, repo: str) -> list[dict]:
    runs = []
    for workflow in ("ci.yml", "train.yml", "release.yml"):
        runs += [run for run in workflow_runs(sh, repo, workflow, 20)
                 if run.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}]
    return runs


def run_detail(sh, repo: str, run_id: str) -> dict | None:
    """`gh run view --json` has no attempt field (gh 2.46); the REST API provides run_attempt."""
    detail = gh_json(sh, "run", "view", str(run_id), "--repo", repo, "--json", RUN_FIELDS + ",jobs")
    if detail is not None:
        result = sh.run(["gh", "api", f"repos/{repo}/actions/runs/{run_id}", "--jq", ".run_attempt"], timeout=30)
        detail["attempt"] = int(result.stdout.strip()) if result.ok and result.stdout.strip().isdigit() else 1
    return detail


def run_log_markers(sh, repo: str, run_id: str, markers: tuple[str, ...]) -> dict[str, bool]:
    result = sh.run(["gh", "run", "view", str(run_id), "--repo", repo, "--log"], timeout=180)
    text = result.stdout if result.ok else ""
    return {marker: marker in text for marker in markers}


def download_artifact(sh, repo: str, run_id: str, name: str, directory: str) -> bool:
    return sh.run(["gh", "run", "download", str(run_id), "--repo", repo, "-n", name, "-D", directory],
                  timeout=120).ok


def gh_variable(sh, repo: str, name: str) -> str | None:
    """gh 2.46 has no `variable get`; list is available on every supported version."""
    variables = gh_json(sh, "variable", "list", "--repo", repo, "--json", "name,value") or []
    return next((item.get("value") for item in variables if item.get("name") == name), None)


def eks_cidrs(sh, cluster: str, region: str) -> list[str] | None:
    result = sh.run(["aws", "eks", "describe-cluster", "--name", cluster, "--region", region, "--query",
                     "cluster.resourcesVpcConfig.publicAccessCidrs", "--output", "json"], timeout=60)
    return sorted(result.json()) if result.ok else None


def hpa_sample(sh) -> dict:
    hpa = kube_json(sh, "-n", "mlops", "get", "hpa", "inference-api") or {}
    pods = kube_json(sh, "-n", "mlops", "get", "pods", "-l", "app=inference-api") or {"items": []}
    claims = kube_json(sh, "get", "nodeclaims") or {"items": []}
    cpu = None
    for metric in hpa.get("status", {}).get("currentMetrics") or []:
        if metric.get("resource", {}).get("name") == "cpu":
            cpu = metric["resource"]["current"].get("averageUtilization")
    target = None
    for metric in hpa.get("spec", {}).get("metrics") or []:
        if metric.get("resource", {}).get("name") == "cpu":
            target = metric["resource"]["target"].get("averageUtilization")
    ready = sum(1 for pod in pods["items"] if any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])))
    return {"cpu_utilization": cpu, "cpu_target": target,
            "current_replicas": hpa.get("status", {}).get("currentReplicas"),
            "desired_replicas": hpa.get("status", {}).get("desiredReplicas"),
            "min_replicas": hpa.get("spec", {}).get("minReplicas"), "max_replicas": hpa.get("spec", {}).get("maxReplicas"),
            "pods_ready": ready, "pods_total": len(pods["items"]),
            "nodeclaims": len(claims["items"]),
            "inference_nodeclaims": sum(1 for c in claims["items"]
                                        if c["metadata"].get("labels", {}).get("karpenter.sh/nodepool", "").startswith("inference"))}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Standard local UI tunnels (e.g. kept alive by `mlops-ui-tunnels`); reused when healthy.
KNOWN_TUNNELS = {("mlops", "mlflow"): "http://127.0.0.1:5000",
                 ("monitoring", "m33-prometheus-server"): "http://127.0.0.1:9090"}
PORT_FORWARD_TIMEOUT = 90  # kubectl on this cluster can need 30-60s (AWS token + websocket upgrade)


def _healthy(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=3).close()
        return True
    except OSError:
        return False


@contextlib.contextmanager
def port_forward(namespace: str, service: str, remote: int, health: str) -> Iterator[str]:
    """Reuse a healthy standard tunnel, else kubectl port-forward on a free port (terminated on exit)."""
    known = KNOWN_TUNNELS.get((namespace, service))
    if known and _healthy(known + health):
        yield known
        return
    import tempfile
    port = _free_port()
    errors = tempfile.TemporaryFile(mode="w+")
    process = subprocess.Popen(["kubectl", "-n", namespace, "port-forward", "--address", "127.0.0.1",
                                f"service/{service}", f"{port}:{remote}"], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=errors)
    url = f"http://127.0.0.1:{port}"

    def reason() -> str:
        errors.seek(0)
        text = errors.read().strip()[-300:]
        return f": {text}" if text else " (expired AWS session / EKS CIDR allow-list?)"
    try:
        deadline = time.monotonic() + PORT_FORWARD_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"port-forward to {service} exited{reason()}")
            try:
                urllib.request.urlopen(url + health, timeout=2).close()
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"port-forward to {service} not ready after {PORT_FORWARD_TIMEOUT}s{reason()}")
        yield url
    finally:
        errors.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


class LiveRegistry:
    """Read-only MLflow Registry access through a cluster port-forward."""

    def __init__(self, uri: str, model_name: str) -> None:
        from mlflow import MlflowClient
        self.client, self.model = MlflowClient(tracking_uri=uri), model_name

    def champion(self) -> dict | None:
        from mlflow.exceptions import MlflowException
        try:
            version = self.client.get_model_version_by_alias(self.model, "champion")
        except MlflowException:
            return None
        return self._version(version)

    @staticmethod
    def _version(version) -> dict:
        tags = version.tags or {}
        return {"version": str(version.version), "status": version.status, "run_id": version.run_id,
                "validation_status": tags.get("validation_status"),
                "validation_mae_minutes": float(tags["validation_mae_minutes"]) if tags.get("validation_mae_minutes") else None,
                "validation_dataset_key": tags.get("validation_dataset_key")}

    def version(self, number: str) -> dict | None:
        from mlflow.exceptions import MlflowException
        try:
            return self._version(self.client.get_model_version(self.model, number))
        except MlflowException:
            return None

    def versions(self) -> list[dict]:
        return [self._version(v) for v in self.client.search_model_versions(f"name='{self.model}'")]

    def run(self, run_id: str) -> dict | None:
        from mlflow.exceptions import MlflowException
        try:
            run = self.client.get_run(run_id)
        except MlflowException:
            return None
        return {"status": run.info.status, "validation_status": run.data.tags.get("validation_status"),
                "validation_mae_minutes": run.data.metrics.get("validation.model.mae_minutes")}


class FixtureRegistry:
    def __init__(self, data: dict) -> None:
        self.data = data

    def champion(self):
        return self.data.get("champion")

    def version(self, number):
        return next((v for v in self.data.get("versions", []) if v["version"] == number), None)

    def versions(self):
        return list(self.data.get("versions", []))

    def run(self, run_id):
        return self.data.get("runs", {}).get(run_id)


@contextlib.contextmanager
def registry(ctx) -> Iterator[Any]:
    if ctx.registry is not None:
        yield ctx.registry
        return
    with port_forward("mlops", "mlflow", 5000, "/health") as url:
        yield LiveRegistry(url, ctx.values["model_name"])


def prometheus_query(url: str, query: str) -> float | None:
    full = url + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(full, timeout=10) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return None
    result = payload.get("data", {}).get("result", [])
    try:
        return float(result[0]["value"][1]) if result else None
    except (KeyError, ValueError, IndexError):
        return None
