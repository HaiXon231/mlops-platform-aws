#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/k8s-common.sh"
check_cluster

# EC2 Fleet/API cannot create this account-wide service-linked role through the
# Karpenter controller in restricted lab accounts. Check it before creating a Job.
if ! spot_role_error="$(aws iam get-role --role-name AWSServiceRoleForEC2Spot \
    --profile "$AWS_PROFILE" --query 'Role.Arn' --output text 2>&1)"; then
  if [[ "$spot_role_error" == *NoSuchEntity* ]]; then
    echo "Missing account-wide IAM role AWSServiceRoleForEC2Spot. An authorized IAM administrator must create it before the Spot smoke test:" >&2
    echo "  aws iam create-service-linked-role --aws-service-name spot.amazonaws.com --profile $AWS_PROFILE" >&2
  else
    echo "Cannot verify AWSServiceRoleForEC2Spot: $spot_role_error" >&2
  fi
  exit 1
fi

if kubectl get job karpenter-training-smoke >/dev/null 2>&1; then
  echo "Smoke Job already exists; delete it before retrying" >&2
  exit 1
fi
if [[ -n "$(kubectl get nodeclaims -l karpenter.sh/nodepool=training-spot -o name)" ]]; then
  echo "Training NodeClaims already exist; smoke test requires an empty training pool" >&2
  exit 1
fi

kubectl apply -f "$ROOT_DIR/deploy/bootstrap/karpenter-smoke-job.yaml"
cleanup_job() { kubectl delete job karpenter-training-smoke --ignore-not-found --wait=true >/dev/null; }
trap cleanup_job EXIT

completed=false
for attempt in {1..60}; do
  if [[ "$(kubectl get job karpenter-training-smoke -o jsonpath='{.status.succeeded}')" == 1 ]]; then
    completed=true
    break
  fi
  if [[ -n "$(kubectl get job karpenter-training-smoke -o jsonpath='{.status.failed}')" ]]; then
    echo "Smoke Job failed; inspect its Pod and Karpenter logs" >&2
    exit 1
  fi
  if (( attempt % 6 == 0 )); then
    echo "Waiting for Spot node and Job completion ($((attempt / 6)) minute(s)); NodeClaims: $(kubectl get nodeclaims -l karpenter.sh/nodepool=training-spot -o name | tr '\n' ' ')"
  fi
  sleep 10
done
[[ "$completed" == true ]] || {
  echo "Smoke Job did not complete within 10 minutes. Recent NodeClaim events:" >&2
  kubectl get events --field-selector involvedObject.kind=NodeClaim --sort-by=.lastTimestamp | tail -15 >&2 || true
  exit 1
}
node="$(kubectl get pods -l job-name=karpenter-training-smoke -o jsonpath='{.items[0].spec.nodeName}')"
[[ -n "$node" ]] && [[ "$(kubectl get node "$node" -o jsonpath='{.metadata.labels.karpenter\.sh/nodepool}')" == training-spot ]] || {
  echo "Smoke Job did not run on a training-spot Karpenter node" >&2; exit 1;
}
kubectl get pods -l job-name=karpenter-training-smoke -o wide
cleanup_job
trap - EXIT

for _ in {1..90}; do
  if [[ -z "$(kubectl get nodeclaims -l karpenter.sh/nodepool=training-spot -o name)" ]] && \
     [[ -z "$(kubectl get nodes -l karpenter.sh/nodepool=training-spot -o name)" ]]; then
    echo "Training node created, Job completed, and Karpenter node removed."
    exit 0
  fi
  sleep 10
done
echo "Training NodeClaim remains after 15 minutes; inspect Karpenter logs and workloads" >&2
exit 1
