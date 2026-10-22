#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM_DIR="$ROOT_DIR/infra/terraform/platform"
AWS_PROFILE="${AWS_PROFILE:-mlops-dev}"
AWS_REGION="${AWS_REGION:-ap-southeast-1}"
CLUSTER_NAME=mlops-aws-dev
KARPENTER_VERSION=1.14.1
ARGOCD_CHART_VERSION=10.10.5

require_command() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

check_cluster() {
  require_command aws
  require_command kubectl
  require_command helm
  require_command terraform
  local status context
  status="$(aws eks describe-cluster --profile "$AWS_PROFILE" --region "$AWS_REGION" --name "$CLUSTER_NAME" --query cluster.status --output text)"
  [[ "$status" == ACTIVE ]] || { echo "EKS cluster $CLUSTER_NAME is $status, expected ACTIVE" >&2; exit 1; }
  context="$(kubectl config current-context)"
  [[ "$context" == *"$CLUSTER_NAME"* ]] || { echo "kubectl context $context does not target $CLUSTER_NAME" >&2; exit 1; }
  kubectl get nodes -o name >/dev/null || { echo "kubectl cannot access nodes in $CLUSTER_NAME" >&2; exit 1; }
}

tf_output() {
  terraform -chdir="$PLATFORM_DIR" output -raw "$1"
}
