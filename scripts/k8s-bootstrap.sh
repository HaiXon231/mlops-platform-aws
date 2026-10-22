#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/k8s-common.sh"
check_cluster

instance_profile="$(tf_output karpenter_node_instance_profile_name)"
queue_name="$(tf_output karpenter_interruption_queue_name)"
[[ "$instance_profile" == "Karpenter-$CLUSTER_NAME" ]] || { echo "Karpenter instance profile differs from EC2NodeClass" >&2; exit 1; }
[[ -n "$queue_name" ]] || { echo "Missing Karpenter interruption queue output" >&2; exit 1; }

helm upgrade --install karpenter-crd oci://public.ecr.aws/karpenter/karpenter-crd \
  --version "$KARPENTER_VERSION" --namespace kube-system --wait --timeout 10m
helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
  --version "$KARPENTER_VERSION" --namespace kube-system --wait --timeout 10m \
  --values "$ROOT_DIR/deploy/platform/karpenter-values.yaml" \
  --set-string "settings.clusterName=$CLUSTER_NAME" \
  --set-string "settings.interruptionQueue=$queue_name"
kubectl apply -f "$ROOT_DIR/deploy/bootstrap/karpenter-nodeclass.yaml"
kubectl apply -f "$ROOT_DIR/deploy/bootstrap/karpenter-nodepools.yaml"
kubectl wait --for=condition=Ready ec2nodeclass/mlops-aws-dev --timeout=5m

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install argocd argo-cd --repo https://argoproj.github.io/argo-helm \
  --version "$ARGOCD_CHART_VERSION" --namespace argocd --wait --timeout 10m \
  --values "$ROOT_DIR/deploy/platform/argocd-values.yaml"
echo "Karpenter and Argo CD bootstrap complete."
