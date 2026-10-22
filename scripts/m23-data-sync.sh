#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
require_command uv
bucket="$(persistent_value bucket_names datasets)"
cd "$ROOT_DIR"
uv run --locked mlops-cloud sync --bucket "$bucket" --config configs/training.yaml --profile "$AWS_PROFILE"
