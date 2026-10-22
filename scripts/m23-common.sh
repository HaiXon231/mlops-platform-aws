#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/k8s-common.sh"
PERSISTENT_DIR="$ROOT_DIR/infra/terraform/persistent"
M23_DIR="$ROOT_DIR/deploy/platform/mlops"

persistent_value() {
  local output="$1" key="$2"
  terraform -chdir="$PERSISTENT_DIR" output -json "$output" | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$key"
}

image_tag() {
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    printf '%s\n' "$IMAGE_TAG"
  else
    (cd "$ROOT_DIR" && sha256sum pyproject.toml uv.lock configs/*.yaml src/training/*.py \
      deploy/training/Dockerfile deploy/mlflow/Dockerfile deploy/mlflow/server.py \
      deploy/backup/Dockerfile deploy/backup/backup.py | sha256sum | cut -d' ' -f1) | sed 's/^/m23-/'
  fi
}

image_url() {
  local purpose="$1" tag
  tag="$(image_tag)"
  if [[ "$purpose" == backup ]]; then purpose=mlflow; fi
  printf '%s:%s\n' "$(persistent_value ecr_repository_urls "$purpose")" "$tag${2:-}"
}

require_pushed_image() {
  local image="$1" repository name tag error
  repository="${image%:*}"
  name="${repository#*/}"
  tag="${image##*:}"
  if ! error="$(aws ecr describe-images --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --repository-name "$name" --image-ids "imageTag=$tag" 2>&1)"; then
    printf 'ECR image is missing or inaccessible: %s\n' "$image" >&2
    printf '%s\n' "$error" >&2
    echo 'Build and push images for this tag before deploying.' >&2
    return 1
  fi
}

render() {
  python3 "$ROOT_DIR/scripts/m23-render.py" "$1"
}

require_mlops_ready() {
  check_cluster
  kubectl -n mlops get statefulset postgres >/dev/null
  kubectl -n mlops rollout status statefulset/postgres --timeout=10m
}

wait_m23_job() {
  local name="$1" timeout_seconds="$2" deadline status next_report=0 connection_failures=0
  deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    if ! status="$(kubectl -n mlops --request-timeout=15s get job "$name" \
      -o jsonpath='{range .status.conditions[*]}{.type}={.status}{" "}{end}' 2>&1)"; then
      if [[ "$status" == *NotFound* ]]; then
        echo "Job $name no longer exists: $status" >&2
        return 1
      fi
      ((connection_failures += 1))
      echo "Cannot query Job $name ($connection_failures/6); retrying: $status" >&2
      if (( connection_failures >= 6 )); then
        echo "Kubernetes API remains unreachable; Job may still be running. Inspect it before retrying the command." >&2
        return 1
      fi
      sleep 10
      continue
    fi
    connection_failures=0
    if [[ "$status" == *'Complete=True'* ]]; then return 0; fi
    if [[ "$status" == *'Failed=True'* ]]; then break; fi
    if (( SECONDS >= next_report )); then
      kubectl -n mlops --request-timeout=15s get job "$name" \
        -o jsonpath='Waiting for Job {.metadata.name}: active={.status.active}, failed={.status.failed}, succeeded={.status.succeeded}{"\n"}' >&2 || true
      next_report=$((SECONDS + 30))
    fi
    sleep 10
  done
  echo "Job $name failed or timed out (conditions: $status)" >&2
  kubectl -n mlops describe "job/$name" >&2 || true
  kubectl -n mlops logs "job/$name" --all-containers=true >&2 || true
  return 1
}

m23_job_logs() {
  local name="$1" attempt
  for attempt in {1..6}; do
    if kubectl -n mlops --request-timeout=20s logs "job/$name" --all-containers=true; then
      return 0
    fi
    echo "Cannot read logs for Job $name ($attempt/6); retrying." >&2
    sleep 5
  done
  echo "Job $name completed, but logs are unavailable; inspect it before retrying the command." >&2
  return 1
}
