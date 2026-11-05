#!/usr/bin/env bash
# Phase 4 add-ons: AWS Load Balancer Controller và Argo Rollouts (không động tới workload suy luận).
set -euo pipefail
source "$(dirname "$0")/k8s-common.sh"

mode="${1:?Use deploy, verify or cleanup}"
ALB_NAMESPACE=kube-system
ALB_RELEASE=aws-load-balancer-controller
ROLLOUTS_NAMESPACE=argo-rollouts
ROLLOUTS_RELEASE=argo-rollouts

deploy() {
  check_cluster
  # Output chỉ có sau khi Terraform platform đã apply IAM/Pod Identity cho controller.
  local role_arn vpc_id
  role_arn="$(tf_output alb_controller_role_arn)"
  [[ "$role_arn" == arn:aws:iam::*:role/"$CLUSTER_NAME"-alb-controller ]] || {
    echo "Terraform output alb_controller_role_arn is missing or unexpected; apply platform first" >&2; exit 1; }
  vpc_id="$(tf_output vpc_id)"
  [[ "$vpc_id" =~ ^vpc-[0-9a-f]+$ ]] || { echo "Unexpected VPC ID: $vpc_id" >&2; exit 1; }

  helm upgrade --install "$ALB_RELEASE" aws-load-balancer-controller \
    --repo https://aws.github.io/eks-charts --version "$ALB_CONTROLLER_CHART_VERSION" \
    --namespace "$ALB_NAMESPACE" --wait --timeout 10m \
    --values "$ROOT_DIR/deploy/platform/aws-load-balancer-controller-values.yaml" \
    --set-string "clusterName=$CLUSTER_NAME" \
    --set-string "region=$AWS_REGION" \
    --set-string "vpcId=$vpc_id"

  kubectl create namespace "$ROLLOUTS_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
  helm upgrade --install "$ROLLOUTS_RELEASE" argo-rollouts \
    --repo https://argoproj.github.io/argo-helm --version "$ARGO_ROLLOUTS_CHART_VERSION" \
    --namespace "$ROLLOUTS_NAMESPACE" --wait --timeout 10m \
    --values "$ROOT_DIR/deploy/platform/argo-rollouts-values.yaml"
  echo "AWS Load Balancer Controller and Argo Rollouts installed."
}

verify() {
  check_cluster
  kubectl -n "$ALB_NAMESPACE" rollout status "deployment/$ALB_RELEASE" --timeout=5m
  kubectl -n "$ROLLOUTS_NAMESPACE" rollout status "deployment/$ROLLOUTS_RELEASE" --timeout=5m
  for crd in rollouts.argoproj.io analysistemplates.argoproj.io analysisruns.argoproj.io; do
    kubectl wait --for=condition=Established "crd/$crd" --timeout=2m
  done
  kubectl get ingressclass alb >/dev/null || { echo "IngressClass alb is missing" >&2; exit 1; }
  # EKS Pod Identity webhook phải đã inject credentials vào Pod controller.
  kubectl -n "$ALB_NAMESPACE" get pods -l "app.kubernetes.io/name=aws-load-balancer-controller" \
    -o jsonpath='{.items[0].spec.containers[0].env[*].name}' | grep -q AWS_CONTAINER_CREDENTIALS_FULL_URI || {
    echo "Pod Identity credentials were not injected; check aws_eks_pod_identity_association" >&2; exit 1; }
  if kubectl -n "$ALB_NAMESPACE" logs "deployment/$ALB_RELEASE" --tail=200 | grep -qi 'AccessDenied\|UnauthorizedOperation'; then
    echo "Load Balancer Controller logs contain AWS authorization errors" >&2; exit 1
  fi
  kubectl -n kube-system get pods -l "app.kubernetes.io/name=aws-load-balancer-controller" -o wide
  kubectl -n "$ROLLOUTS_NAMESPACE" get pods -o wide
  echo "Phase 4 add-ons verified"
}

cleanup() {
  check_cluster
  # Gỡ controller khi còn Ingress ALB hoặc Rollout sẽ để lại ALB tính phí và finalizer treo.
  local ingresses rollouts
  ingresses="$(kubectl get ingress -A -o jsonpath='{range .items[?(@.spec.ingressClassName=="alb")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}')"
  rollouts="$(kubectl get rollouts.argoproj.io -A -o name 2>/dev/null || true)"
  if [[ -n "$ingresses" || -n "$rollouts" ]]; then
    echo "Refusing cleanup while ALB Ingress or Rollout objects exist (remove them through GitOps first):" >&2
    printf '%s\n%s\n' "$ingresses" "$rollouts" | sed '/^$/d' >&2
    exit 1
  fi
  helm -n "$ROLLOUTS_NAMESPACE" uninstall "$ROLLOUTS_RELEASE" --ignore-not-found
  helm -n "$ALB_NAMESPACE" uninstall "$ALB_RELEASE" --ignore-not-found
  echo "Add-ons removed. Argo Rollouts CRDs are kept by Helm; delete them only after no Rollout exists."
}

case "$mode" in
  deploy) deploy ;;
  verify) verify ;;
  cleanup) cleanup ;;
  *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac
