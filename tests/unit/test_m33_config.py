"""Guard the HPA ownership and limited monitoring scope across future releases."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def read_yaml(path: str):
    return yaml.safe_load((ROOT / path).read_text())


def test_hpa_and_argocd_agree_on_replica_ownership():
    hpa = read_yaml("deploy/gitops/inference/hpa.yaml")
    app = read_yaml("deploy/argocd/inference-application-automated.yaml")
    kustomization = read_yaml("deploy/gitops/inference/kustomization.yaml")
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "apps/v1", "kind": "Deployment", "name": "inference-api"
    }
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (1, 3)
    assert hpa["spec"]["metrics"][0]["resource"] == {
        "name": "cpu", "target": {"type": "Utilization", "averageUtilization": 60}
    }
    assert "hpa.yaml" in kustomization["resources"]
    assert any("/spec/replicas" in item.get("jsonPointers", []) for item in app["spec"]["ignoreDifferences"])
    assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]
    assert app["spec"]["syncPolicy"]["automated"] == {"prune": False, "selfHeal": True}


def test_monitoring_is_private_ephemeral_and_scrapes_inference_pods_only():
    prom = read_yaml("deploy/monitoring/prometheus-values.yaml")
    grafana = read_yaml("deploy/monitoring/grafana-values.yaml")
    assert prom["server"]["persistentVolume"]["enabled"] is False
    assert prom["server"]["retention"] == "24h"
    assert prom["server"]["service"]["type"] == grafana["service"]["type"] == "ClusterIP"
    assert not prom["rbac"]["create"]
    assert not grafana["rbac"]["create"]
    assert all(not prom[name]["enabled"] for name in (
        "alertmanager", "kube-state-metrics", "prometheus-node-exporter", "prometheus-pushgateway"
    ))
    jobs = prom["serverFiles"]["prometheus.yml"]["scrape_configs"]
    assert len(jobs) == 1 and jobs[0]["job_name"] == "inference-api"
    assert jobs[0]["kubernetes_sd_configs"][0]["namespaces"]["names"] == ["mlops"]
    assert grafana["admin"]["existingSecret"] == "m33-grafana-admin"
    assert grafana["deploymentStrategy"]["type"] == "Recreate"
    assert grafana["dashboardProviders"]["dashboardproviders.yaml"]["providers"][0]["updateIntervalSeconds"] > 10
    assert grafana["datasources"]["datasources.yaml"]["datasources"][0]["url"].startswith(
        "http://m33-prometheus-server.monitoring.svc.cluster.local"
    )


def test_dashboard_matches_serving_metrics_contract():
    dashboard = json.loads((ROOT / "deploy/monitoring/inference-dashboard.json").read_text())
    queries = [panel["targets"][0]["expr"] for panel in dashboard["panels"]]
    assert len(queries) == 4
    assert any("inference_http_requests_total" in query for query in queries)
    assert any("histogram_quantile" in query and "inference_http_request_duration_seconds_bucket" in query for query in queries)
    assert any("inference_http_errors_total" in query and "or vector(0)" in query for query in queries)
    assert any('up{job="inference-api"}' in query for query in queries)


def test_discovery_rbac_and_load_job_stay_in_scope():
    rbac = list(yaml.safe_load_all((ROOT / "deploy/monitoring/prometheus-rbac.yaml").read_text()))
    role = next(item for item in rbac if item["kind"] == "Role")
    binding = next(item for item in rbac if item["kind"] == "RoleBinding")
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "mlops"
    assert role["rules"] == [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}]
    assert binding["subjects"][0]["namespace"] == "monitoring"

    template = (ROOT / "deploy/monitoring/k6-job.yaml.tpl").read_text()
    for key, value in {"K6_VUS": "10", "K6_RATE": "20", "K6_DURATION": "300s"}.items():
        template = template.replace("${" + key + "}", value)
    job = yaml.safe_load(template)
    assert job["metadata"]["namespace"] == "mlops"
    assert job["metadata"]["labels"]["app"] == "m33-k6"
    assert job["spec"]["template"]["spec"]["nodeSelector"] == {"eks.amazonaws.com/nodegroup": "system"}
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["cpu"] == "500m"
    assert container["resources"]["limits"]["memory"] == "512Mi"
