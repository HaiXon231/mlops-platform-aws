#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
check_cluster
require_command uv

addon="$(aws eks describe-addon --profile "$AWS_PROFILE" --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
  --addon-name aws-ebs-csi-driver --query addon.status --output text)"
[[ "$addon" == ACTIVE ]] || { echo "EBS CSI add-on is $addon" >&2; exit 1; }
kubectl wait nodes -l eks.amazonaws.com/nodegroup=system --for=condition=Ready --timeout=5m
kubectl -n mlops rollout status statefulset/postgres --timeout=10m
kubectl -n mlops rollout status deployment/mlflow --timeout=10m
[[ "$(kubectl -n mlops get pvc data-postgres-0 -o jsonpath='{.status.phase}')" == Bound ]] || {
  echo 'PostgreSQL PVC is not Bound' >&2; exit 1;
}
kubectl -n mlops get pods,pvc
kubectl describe nodes -l eks.amazonaws.com/nodegroup=system | sed -n '/Allocated resources:/,/Events:/p'

kubectl -n mlops port-forward service/mlflow 15000:5000 >/tmp/m23-port-forward-$$.log 2>&1 &
forward_pid=$!
trap 'kill "$forward_pid" 2>/dev/null || true; rm -f "/tmp/m23-port-forward-$$.log"' EXIT
for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:15000/health >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:15000/health >/dev/null
curl -fsS -H 'Host: mlflow.mlops.svc.cluster.local:5000' \
  -H 'Content-Type: application/json' --data '{"max_results":1}' \
  http://127.0.0.1:15000/api/2.0/mlflow/experiments/search >/dev/null
cd "$ROOT_DIR"
uv run --locked mlops-train tracking-verify --tracking-uri http://127.0.0.1:15000

if [[ "${VERIFY_TRAINING:-0}" == 1 ]]; then
  completed="$(kubectl -n mlops get jobs -l app=mlops-training -o jsonpath='{range .items[*]}{.status.succeeded}{"\n"}{end}' | grep -c '^1$' || true)"
  [[ "$completed" -gt 0 ]] || { echo 'No completed training Job found' >&2; exit 1; }
  uv run --locked python -c 'import mlflow, mlflow.lightgbm; from mlflow import MlflowClient; mlflow.set_tracking_uri("http://127.0.0.1:15000"); versions=list(MlflowClient().search_model_versions("name = '\''nyc-taxi-trip-duration'\''")); assert versions, "No registered model version"; chosen=max(versions, key=lambda v: int(v.version)); mlflow.lightgbm.load_model(f"models:/nyc-taxi-trip-duration/{chosen.version}"); print("Loaded registered model version", chosen.version)'
  bucket="$(persistent_value bucket_names model_artifacts)"
  artifact_key="$(aws s3api list-objects-v2 --profile "$AWS_PROFILE" --region "$AWS_REGION" --bucket "$bucket" \
    --prefix mlflow/ --max-items 1 --query 'Contents[0].Key' --output text)"
  [[ "$artifact_key" != None ]] || { echo 'No MLflow artifacts in S3' >&2; exit 1; }
  for _ in {1..60}; do
    [[ -z "$(kubectl get nodeclaims -l karpenter.sh/nodepool=training-spot -o name)" ]] && break
    sleep 10
  done
  [[ -z "$(kubectl get nodeclaims -l karpenter.sh/nodepool=training-spot -o name)" ]] || {
    echo 'Spot NodeClaim still present after training; inspect pods and Karpenter consolidation' >&2; exit 1;
  }
fi
echo 'M2.3 platform verification passed.'
