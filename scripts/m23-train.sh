#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
require_mlops_ready
kubectl -n mlops rollout status deployment/mlflow --timeout=10m
export TRAINING_IMAGE DATA_BUCKET RUN_SUFFIX
TRAINING_IMAGE="$(image_url training)"
require_pushed_image "$TRAINING_IMAGE"
DATA_BUCKET="$(persistent_value bucket_names datasets)"
RUN_SUFFIX="$(date -u +%Y%m%d%H%M%S)-$$"
name="mlops-training-$RUN_SUFFIX"
render "$M23_DIR/training-job.yaml.tpl" | kubectl apply -f -
wait_m23_job "$name" 7800
m23_job_logs "$name"
echo "Completed Kubernetes Job $name; inspect Registry with make m23-verify"
