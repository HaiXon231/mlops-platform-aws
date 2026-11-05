#!/usr/bin/env bash
# Quan sát canary: trạng thái Rollout và tỷ lệ request giữa stable/canary (bằng chứng cho 90/10).
set -Eeuo pipefail

mode="${1:?Use status or split}"

require_cluster() {
  local context
  context="$(kubectl config current-context)"
  [[ "$context" == *mlops-aws-dev* ]] || { echo "Wrong kubectl context: $context" >&2; exit 1; }
}

status() {
  require_cluster
  kubectl -n mlops get rollout.argoproj.io inference-api -o wide
  kubectl -n mlops get analysisrun.argoproj.io --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -n 6 || true
  kubectl -n mlops get ingress inference-api -o wide
  kubectl -n mlops get pods -l app=inference-api -L rollouts-pod-template-hash
}

split() {
  require_cluster
  local window="${M42_WINDOW:-2m}"
  [[ "$window" =~ ^[0-9]+[smh]$ ]] || { echo 'M42_WINDOW must look like 90s, 2m or 1h' >&2; exit 2; }
  kubectl -n monitoring port-forward --address 127.0.0.1 service/m33-prometheus-server 19091:80 \
    >/tmp/m42-prometheus-port-forward.log 2>&1 &
  local pid=$!
  # Chuỗi trong dấu nháy kép: biến local không còn tồn tại khi trap EXIT chạy, nên phải mở rộng ngay.
  trap "kill $pid 2>/dev/null || true" EXIT
  for _ in $(seq 1 20); do
    curl -fsS --max-time 2 http://127.0.0.1:19091/-/ready >/dev/null 2>&1 && break
    sleep 1
  done
  local stable canary
  stable="$(kubectl -n mlops get rollout.argoproj.io inference-api -o jsonpath='{.status.stableRS}')"
  canary="$(kubectl -n mlops get rollout.argoproj.io inference-api -o jsonpath='{.status.currentPodHash}')"
  curl -fsS --get --max-time 10 \
    --data-urlencode "query=sum by (rollouts_pod_template_hash) (increase(inference_http_requests_total{path=\"/predict\"}[$window]))" \
    http://127.0.0.1:19091/api/v1/query \
    | python3 "$(dirname "${BASH_SOURCE[0]}")/m42_split.py" --stable "$stable" --current "$canary" --window "$window"
}

case "$mode" in
  status) status ;;
  split) split ;;
  *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac
