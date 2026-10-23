#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
monitor="$root/deploy/monitoring"
mode="${1:?Use deploy, verify, load-test or cleanup}"

require_cluster() {
  local context
  context="$(kubectl config current-context)"
  [[ "$context" == *mlops-aws-dev* ]] || { echo "Wrong kubectl context: $context" >&2; exit 1; }
}

deploy() {
  : "${GRAFANA_ADMIN_PASSWORD:?Set GRAFANA_ADMIN_PASSWORD outside Git before deployment}"
  require_cluster
  kubectl -n kube-system rollout status deployment/metrics-server --timeout=5m
  kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f "$monitor/prometheus-rbac.yaml"
  printf '%s' "$GRAFANA_ADMIN_PASSWORD" | kubectl -n monitoring create secret generic m33-grafana-admin \
    --from-literal=admin-user=admin --from-file=admin-password=/dev/stdin \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n monitoring create configmap m33-inference-dashboard \
    --from-file=inference-dashboard.json="$monitor/inference-dashboard.json" \
    --dry-run=client -o yaml | kubectl apply -f -
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
  helm repo add grafana-community https://grafana-community.github.io/helm-charts --force-update
  helm repo update prometheus-community grafana-community
  helm upgrade --install m33-prometheus prometheus-community/prometheus --version 29.37.1 \
    --namespace monitoring --values "$monitor/prometheus-values.yaml" --wait --timeout 10m
  helm upgrade --install m33-grafana grafana-community/grafana --version 14.0.1 \
    --namespace monitoring --values "$monitor/grafana-values.yaml" --wait --timeout 10m
}

verify() {
  require_cluster
  kubectl -n kube-system rollout status deployment/metrics-server --timeout=5m
  kubectl -n mlops get hpa inference-api
  kubectl -n mlops get hpa inference-api -o json | python3 -c \
    'import json,sys; x=json.load(sys.stdin); assert any(m.get("resource",{}).get("name")=="cpu" and m["resource"]["current"].get("averageUtilization") is not None for m in x["status"].get("currentMetrics",[])), "HPA CPU metric is unavailable"'
  kubectl -n mlops top pods -l app=inference-api
  kubectl -n mlops get pods -l app=inference-api -o wide
  kubectl get nodeclaims
  helm -n monitoring status m33-prometheus >/dev/null
  helm -n monitoring status m33-grafana >/dev/null
  kubectl -n monitoring port-forward --address 127.0.0.1 service/m33-prometheus-server 19090:80 >/tmp/m33-prometheus-port-forward.log 2>&1 &
  local prom_pid=$!
  kubectl -n monitoring port-forward --address 127.0.0.1 service/m33-grafana 13000:80 >/tmp/m33-grafana-port-forward.log 2>&1 &
  local grafana_pid=$!
  trap "kill $prom_pid $grafana_pid 2>/dev/null || true" EXIT
  local attempt replicas
  replicas="$(kubectl -n mlops get deployment inference-api -o jsonpath='{.spec.replicas}')"
  for attempt in $(seq 1 24); do
    if curl -fsS --get --data-urlencode 'query=up{job="inference-api"}' \
        http://127.0.0.1:19090/api/v1/query | python3 -c \
        'import json,sys; x=json.load(sys.stdin); r=x["data"]["result"]; assert x["status"]=="success" and len(r)==int(sys.argv[1]) and all(float(i["value"][1])==1 for i in r)' "$replicas" 2>/dev/null; then
      break
    fi
    if [[ "$attempt" -eq 24 ]]; then echo 'Prometheus inference targets not UP' >&2; return 1; fi
    sleep 5
  done
  local password
  password="$(kubectl -n monitoring get secret m33-grafana-admin -o jsonpath='{.data.admin-password}' | base64 --decode)"
  curl -fsS --user "admin:$password" http://127.0.0.1:13000/api/datasources/uid/prometheus | python3 -c \
    'import json,sys; x=json.load(sys.stdin); assert x["url"]=="http://m33-prometheus-server.monitoring.svc.cluster.local:80"'
  curl -fsS --user "admin:$password" http://127.0.0.1:13000/api/datasources/uid/prometheus/health | python3 -c \
    'import json,sys; x=json.load(sys.stdin); assert x["status"].upper()=="OK", x'
  unset password
  echo 'Metrics Server, HPA, inference Pod CPU, Prometheus targets and Grafana datasource verified'
}

load_test() {
  require_cluster
  local vus="${M33_VUS:-10}" rate="${M33_RATE:-20}" seconds="${M33_DURATION_SECONDS:-300}"
  [[ "$vus" =~ ^[0-9]+$ && "$rate" =~ ^[0-9]+$ && "$seconds" =~ ^[0-9]+$ ]] || { echo 'Load parameters must be integers' >&2; exit 2; }
  (( vus >= 1 && vus <= 50 && rate >= 1 && rate <= 200 && seconds >= 30 && seconds <= 900 )) || {
    echo 'Limits: VUS 1-50, RATE 1-200 requests/s, DURATION_SECONDS 30-900' >&2; exit 2;
  }
  kubectl -n mlops create configmap m33-k6-script --from-file=k6.js="$monitor/k6.js" \
    --dry-run=client -o yaml | kubectl apply -f -
  local job
  job="$(K6_VUS="$vus" K6_RATE="$rate" K6_DURATION="${seconds}s" python3 - "$monitor/k6-job.yaml.tpl" <<'PY' | kubectl create -f - -o name
import os, pathlib, sys
content = pathlib.Path(sys.argv[1]).read_text()
for key in ("K6_VUS", "K6_RATE", "K6_DURATION"):
    content = content.replace("${" + key + "}", os.environ[key])
print(content)
PY
)"
  echo "Load test: $job. Watch HPA with: kubectl -n mlops get hpa inference-api -w"
  kubectl -n mlops wait --for=condition=complete "$job" --timeout="$((seconds + 120))s" || {
    kubectl -n mlops logs "$job" --all-containers=true || true
    return 1
  }
  kubectl -n mlops logs "$job" --all-containers=true
}

cleanup() {
  require_cluster
  kubectl -n mlops delete jobs -l app=m33-k6 --ignore-not-found
  kubectl -n mlops delete configmap m33-k6-script --ignore-not-found
  helm -n monitoring uninstall m33-grafana --ignore-not-found
  helm -n monitoring uninstall m33-prometheus --ignore-not-found
  kubectl -n monitoring delete configmap m33-inference-dashboard --ignore-not-found
  kubectl -n monitoring delete secret m33-grafana-admin --ignore-not-found
  kubectl delete -f "$monitor/prometheus-rbac.yaml" --ignore-not-found
  echo 'Monitoring and load-test resources removed; GitOps inference and Metrics Server remain'
}

case "$mode" in
  deploy) deploy ;;
  verify) verify ;;
  load-test) load_test ;;
  cleanup) cleanup ;;
  *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac
