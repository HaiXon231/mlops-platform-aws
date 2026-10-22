#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

mode="${1:?Use build, push, deploy, verify or cleanup}"
release_dir="${M31_RELEASE_DIR:-artifacts/releases/nyc-taxi-trip-duration-v1}"
manifest="deploy/gitops/inference/deployment.yaml"

image_tag() {
  local checksum code_hash
  checksum="$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_checksum"][:16])' "$release_dir/release.json")"
  code_hash="$(sha256sum pyproject.toml uv.lock deploy/inference/Dockerfile src/inference/*.py src/training/*.py | sha256sum | cut -c1-12)"
  printf 'm31-v1-%s-%s\n' "$checksum" "$code_hash"
}

case "$mode" in
  build)
    .venv/bin/python - "$release_dir" <<'PY'
from pathlib import Path
from inference.bundle import validate_release
import sys
metadata, _ = validate_release(Path(sys.argv[1]))
assert metadata["model_name"] == "nyc-taxi-trip-duration" and metadata["model_version"] == "1"
print(f"Validated {metadata['model_name']} v{metadata['model_version']} ({metadata['artifact_checksum']})")
PY
    tag="$(image_tag)"
    docker build -f deploy/inference/Dockerfile --build-arg "RELEASE_DIR=$release_dir" -t "mlops-inference:$tag" .
    printf 'Built mlops-inference:%s\n' "$tag"
    ;;
  push)
    tag="${M31_IMAGE_TAG:-$(image_tag)}"
    checksum="$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_checksum"][:16])' "$release_dir/release.json")"
    if [[ ! "$tag" =~ ^m31-v1-${checksum}-[0-9a-f]{12}$ ]]; then
      echo "Image tag $tag does not match this release bundle checksum $checksum" >&2
      exit 1
    fi
    if ! docker image inspect "mlops-inference:$tag" >/dev/null 2>&1; then
      printf 'Local image mlops-inference:%s is missing. Run make m31-image-build or set M31_IMAGE_TAG to an existing tested tag.\n' "$tag" >&2
      docker image ls mlops-inference --format '{{.Repository}}:{{.Tag}}' >&2
      exit 1
    fi
    repository="$(terraform -chdir=infra/terraform/persistent output -json ecr_repository_urls | python3 -c 'import json,sys; print(json.load(sys.stdin)["inference"])')"
    account="$(aws sts get-caller-identity --profile "${AWS_PROFILE:-mlops-dev}" --query Account --output text)"
    aws ecr get-login-password --profile "${AWS_PROFILE:-mlops-dev}" --region ap-southeast-1 | docker login --username AWS --password-stdin "$account.dkr.ecr.ap-southeast-1.amazonaws.com"
    docker tag "mlops-inference:$tag" "$repository:$tag"
    docker push "$repository:$tag"
    digest="$(aws ecr describe-images --profile "${AWS_PROFILE:-mlops-dev}" --region ap-southeast-1 --repository-name "${repository##*/}" --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text)"
    printf 'Immutable image: %s@%s\n' "$repository" "$digest"
    printf 'Pin this digest in %s before deploying.\n' "$manifest"
    ;;
  deploy)
    .venv/bin/python scripts/m31-validate-manifest.py
    kubectl apply -k deploy/gitops/inference
    kubectl -n mlops rollout status deployment/inference-api --timeout=10m
    ;;
  verify)
    .venv/bin/python scripts/m31-validate-manifest.py
    kubectl -n mlops rollout status deployment/inference-api --timeout=10m
    kubectl -n mlops get deployment/inference-api service/inference-api -o wide
    ;;
  cleanup)
    kubectl -n mlops delete deployment/inference-api service/inference-api --ignore-not-found
    ;;
  *) echo "Use build, push, deploy, verify or cleanup" >&2; exit 2 ;;
esac
