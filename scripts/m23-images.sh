#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
require_command docker
mode="${1:?Use build or push}"
tag="$(image_tag)"

case "$mode" in
  build)
    revision="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"
    docker build -f "$ROOT_DIR/deploy/training/Dockerfile" --build-arg "GIT_SHA=$revision" \
      --build-arg "SOURCE_TREE_SHA256=${tag#m23-}" -t "mlops-m23-training:$tag" "$ROOT_DIR"
    docker build -f "$ROOT_DIR/deploy/mlflow/Dockerfile" -t "mlops-m23-mlflow:$tag" "$ROOT_DIR"
    docker build -f "$ROOT_DIR/deploy/backup/Dockerfile" -t "mlops-m23-backup:$tag" "$ROOT_DIR"
    printf 'Built local images with tag %s\n' "$tag"
    ;;
  push)
    require_command aws
    account="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)"
    aws ecr get-login-password --profile "$AWS_PROFILE" --region "$AWS_REGION" | \
      docker login --username AWS --password-stdin "$account.dkr.ecr.$AWS_REGION.amazonaws.com"
    for purpose in training mlflow backup; do
      local_image="mlops-m23-$purpose:$tag"
      docker image inspect "$local_image" >/dev/null
      suffix=""
      if [[ "$purpose" == backup ]]; then suffix=-backup; fi
      remote_image="$(image_url "$purpose" "$suffix")"
      repository="${remote_image%:*}"
      image_name="${repository#*/}"
      remote_tag="${remote_image##*:}"
      if aws ecr describe-images --profile "$AWS_PROFILE" --region "$AWS_REGION" \
        --repository-name "$image_name" --image-ids "imageTag=$remote_tag" >/dev/null 2>&1; then
        printf 'Already present (immutable tag): %s\n' "$remote_image"
        continue
      fi
      docker tag "$local_image" "$remote_image"
      docker push "$remote_image"
      printf '%s\n' "$remote_image"
    done
    ;;
  *) echo "Use build or push" >&2; exit 2 ;;
esac
