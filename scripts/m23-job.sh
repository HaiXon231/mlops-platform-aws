#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
mode="${1:?Use backup, verify or restore}"
key="${2:-}"
[[ "$mode" =~ ^(backup|verify|restore)$ ]] || { echo "Invalid backup mode: $mode" >&2; exit 2; }
if [[ "$mode" != backup && -z "$key" ]]; then echo 'Provide BACKUP_KEY=postgres/<file>.dump' >&2; exit 2; fi
require_mlops_ready
if [[ "$mode" == restore ]] && kubectl -n mlops get deployment mlflow >/dev/null 2>&1; then
  echo 'Stop/remove MLflow Deployment before restoring the primary database' >&2
  exit 1
fi
export BACKUP_MODE="$mode" BACKUP_KEY="$key" BACKUP_BUCKET
BACKUP_BUCKET="$(persistent_value bucket_names mlflow_backups)"
export BACKUP_IMAGE
BACKUP_IMAGE="$(image_url backup -backup)"
require_pushed_image "$BACKUP_IMAGE"
BACKUP_JOB_SUFFIX="${mode}-$(date -u +%Y%m%d%H%M%S)-$$"
export BACKUP_JOB_SUFFIX
name="mlops-backup-$BACKUP_JOB_SUFFIX"
render "$M23_DIR/backup-job.yaml.tpl" | kubectl apply -f -
wait_m23_job "$name" 2100
m23_job_logs "$name"
