#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
check_cluster
require_command python3
MLFLOW_IMAGE="$(image_url mlflow)"
require_pushed_image "$MLFLOW_IMAGE"
if kubectl -n mlops get secret mlflow-db >/dev/null 2>&1; then
  if [[ -n "${MLFLOW_DB_PASSWORD:-}" ]]; then
    saved_password="$(kubectl -n mlops get secret mlflow-db -o jsonpath='{.data.password}' | base64 -d)"
    [[ "$saved_password" == "$MLFLOW_DB_PASSWORD" ]] || { echo 'Existing Kubernetes database password differs; refusing implicit rotation' >&2; exit 1; }
    unset saved_password
  fi
else
  [[ -n "${MLFLOW_DB_PASSWORD:-}" ]] || { echo 'Set MLFLOW_DB_PASSWORD in the shell (not in Git)' >&2; exit 1; }
  [[ "${#MLFLOW_DB_PASSWORD}" -ge 16 ]] || { echo 'MLFLOW_DB_PASSWORD must contain at least 16 characters' >&2; exit 1; }
fi
if [[ -n "${RESTORE_KEY:-}" ]] && kubectl -n mlops get deployment mlflow >/dev/null 2>&1; then
  echo 'MLflow is already deployed; remove it before primary restore' >&2
  exit 1
fi

# A new PVC with existing S3 backups must be restored before MLflow starts.
if ! kubectl -n mlops get pvc data-postgres-0 >/dev/null 2>&1; then
  backup_bucket="$(persistent_value bucket_names mlflow_backups)"
  existing="$(aws s3api list-objects-v2 --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --bucket "$backup_bucket" --prefix postgres/ --max-items 1 --query 'Contents[0].Key' --output text)"
  if [[ "$existing" != None && -z "${RESTORE_KEY:-}" ]]; then
    echo 'S3 PostgreSQL backups exist. Set RESTORE_KEY=postgres/<file>.dump before creating a fresh database.' >&2
    exit 1
  fi
fi

kubectl apply -f "$M23_DIR/namespace.yaml"
kubectl apply -f "$M23_DIR/storageclass.yaml"
kubectl apply -f "$M23_DIR/serviceaccounts.yaml"
if ! kubectl -n mlops get secret mlflow-db >/dev/null 2>&1; then
  printf '%s' "$MLFLOW_DB_PASSWORD" | kubectl -n mlops create secret generic mlflow-db --from-file=password=/dev/stdin \
    --dry-run=client -o yaml | kubectl apply -f -
fi
kubectl apply -f "$M23_DIR/postgres.yaml"
kubectl -n mlops rollout status statefulset/postgres --timeout=15m

if [[ -n "${RESTORE_KEY:-}" ]]; then
  if kubectl -n mlops get deployment mlflow >/dev/null 2>&1; then
    echo 'MLflow is already deployed; refusing automatic primary restore' >&2
    exit 1
  fi
  bash "$ROOT_DIR/scripts/m23-job.sh" verify "$RESTORE_KEY"
  bash "$ROOT_DIR/scripts/m23-job.sh" restore "$RESTORE_KEY"
fi

export MLFLOW_IMAGE MODEL_BUCKET
MODEL_BUCKET="$(persistent_value bucket_names model_artifacts)"
render "$M23_DIR/mlflow.yaml.tpl" | kubectl apply -f -
kubectl -n mlops rollout status deployment/mlflow --timeout=15m
echo 'MLflow Ready. Access UI with: make m23-port-forward'
