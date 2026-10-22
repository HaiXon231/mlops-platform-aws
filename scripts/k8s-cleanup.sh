#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/k8s-common.sh"
check_cluster
require_command python3
if kubectl get namespace mlops >/dev/null 2>&1 && \
   { kubectl -n mlops get pvc data-postgres-0 >/dev/null 2>&1 || kubectl -n mlops get statefulset postgres >/dev/null 2>&1; }; then
  echo 'M2.3 PostgreSQL storage still exists; run make m23-teardown (backup and restore verification) first.' >&2
  exit 1
fi

if kubectl get namespace argocd >/dev/null 2>&1 && kubectl get crd applications.argoproj.io >/dev/null 2>&1; then
  other_apps="$(kubectl -n argocd get applications.argoproj.io -o name 2>/dev/null | sed '/application.argoproj.io\/mlops-gitops-smoke$/d' || true)"
  [[ -z "$other_apps" ]] || { echo "Other Argo CD Applications remain; remove them and their workloads first: $other_apps" >&2; exit 1; }
  kubectl -n argocd delete application mlops-gitops-smoke --ignore-not-found --wait=true
fi
kubectl delete configmap argocd-gitops-smoke --ignore-not-found
kubectl delete job karpenter-training-smoke --ignore-not-found --wait=true

if kubectl get crd nodepools.karpenter.sh >/dev/null 2>&1; then
  other_pools="$(kubectl get nodepools -o name | sed -e '/nodepool.karpenter.sh\/training-spot$/d' -e '/nodepool.karpenter.sh\/inference-on-demand$/d')"
  [[ -z "$other_pools" ]] || { echo "Other NodePools remain; remove them explicitly first: $other_pools" >&2; exit 1; }
  kubectl get nodes -l karpenter.sh/nodepool -o json | \
    python3 -c 'import json,subprocess,sys; nodes={n["metadata"]["name"] for n in json.load(sys.stdin)["items"]}; pods=json.loads(subprocess.check_output(["kubectl","get","pods","-A","-o","json"]))["items"]; busy=["{}/{}".format(p["metadata"]["namespace"],p["metadata"]["name"]) for p in pods if p["spec"].get("nodeName") in nodes and p["status"].get("phase") not in ("Succeeded","Failed") and not any(o.get("kind")=="DaemonSet" for o in p["metadata"].get("ownerReferences",[]))]; print("Workloads still run on Karpenter nodes: "+", ".join(busy),file=sys.stderr) if busy else None; sys.exit(bool(busy))'
  kubectl delete nodepool training-spot inference-on-demand --ignore-not-found --wait=false
  for _ in {1..90}; do
    if [[ -z "$(kubectl get nodeclaims -o name)" ]]; then break; fi
    sleep 10
  done
  [[ -z "$(kubectl get nodeclaims -o name)" ]] || { echo "Karpenter NodeClaims remain; aborting cleanup before uninstall" >&2; exit 1; }
  kubectl delete ec2nodeclass mlops-aws-dev --ignore-not-found --wait=true
fi

for release in karpenter karpenter-crd; do
  if helm status "$release" -n kube-system >/dev/null 2>&1; then
    helm uninstall "$release" -n kube-system --wait --timeout 10m
  fi
done
if helm status argocd -n argocd >/dev/null 2>&1; then
  helm uninstall argocd -n argocd --wait --timeout 10m
fi
kubectl delete namespace argocd --ignore-not-found --wait=true
echo "Kubernetes bootstrap cleanup complete. Review AWS resources before Terraform destroy."
