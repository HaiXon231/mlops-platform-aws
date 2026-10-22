#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
check_cluster
if ! kubectl get namespace mlops >/dev/null 2>&1; then
  echo 'M2.3 namespace absent; nothing to tear down.'
  exit 0
fi

if kubectl -n mlops get statefulset postgres >/dev/null 2>&1 && \
   ! kubectl -n mlops get pvc data-postgres-0 >/dev/null 2>&1; then
  echo 'PostgreSQL StatefulSet exists without its PVC; inspect data before teardown' >&2
  exit 1
fi

if kubectl -n mlops get pvc data-postgres-0 >/dev/null 2>&1; then
  kubectl -n mlops get statefulset postgres >/dev/null || {
    echo 'PostgreSQL PVC exists without StatefulSet; restore service and back up before teardown' >&2; exit 1;
  }
  echo 'Backing up PostgreSQL before removing Kubernetes storage.'
  backup_log="$(bash "$ROOT_DIR/scripts/m23-job.sh" backup)" || exit $?
  printf '%s\n' "$backup_log"
  key="$(printf '%s\n' "$backup_log" | sed -n 's/^BACKUP_KEY=//p' | tail -n1)"
  [[ -n "$key" ]] || { echo 'Backup Job did not return an S3 key; stopping teardown' >&2; exit 1; }
  bash "$ROOT_DIR/scripts/m23-job.sh" verify "$key"
  mkdir -p "$ROOT_DIR/backups"
  printf '%s\n' "$key" > "$ROOT_DIR/backups/cloud-last-key.txt"
  echo "Verified S3 backup: $key"
fi

kubectl -n mlops delete jobs -l app=mlops-training --ignore-not-found --wait=true
kubectl -n mlops delete deployment mlflow --ignore-not-found --wait=true
kubectl -n mlops delete statefulset postgres --ignore-not-found --wait=true
if kubectl -n mlops get pvc data-postgres-0 >/dev/null 2>&1; then
  pv="$(kubectl -n mlops get pvc data-postgres-0 -o jsonpath='{.spec.volumeName}')"
  [[ -n "$pv" ]] || { echo 'PVC has no bound PV; inspect it before deletion' >&2; exit 1; }
  kubectl -n mlops delete pvc data-postgres-0 --wait=true
  kubectl wait --for=delete "pv/$pv" --timeout=10m || {
    echo "PV $pv remains; inspect EBS CSI volume before destroying EKS" >&2; exit 1;
  }
fi
kubectl delete namespace mlops --wait=true
kubectl delete storageclass mlops-gp3 --ignore-not-found
echo 'M2.3 resources removed; persistent S3/ECR resources were untouched.'
