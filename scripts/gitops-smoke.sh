#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/k8s-common.sh"
check_cluster
require_command python3
: "${GITOPS_REPO_URL:?Set GITOPS_REPO_URL to an accessible Git repository URL}"
GITOPS_REVISION="${GITOPS_REVISION:-main}"
export GITOPS_REPO_URL GITOPS_REVISION
[[ "$GITOPS_REPO_URL" == https://* || "$GITOPS_REPO_URL" == ssh://* || "$GITOPS_REPO_URL" == git@* ]] || {
  echo "GITOPS_REPO_URL must be a Git HTTPS or SSH URL" >&2; exit 1;
}
[[ "$GITOPS_REPO_URL" != *://*@* ]] || { echo "Do not embed Git credentials in GITOPS_REPO_URL" >&2; exit 1; }

GITOPS_TEMPLATE="$ROOT_DIR/deploy/gitops/smoke-application.yaml.tpl" \
  python3 -c 'import json,os,pathlib,sys; s=pathlib.Path(os.environ["GITOPS_TEMPLATE"]).read_text(); s=s.replace("${GITOPS_REPO_URL}",json.dumps(os.environ["GITOPS_REPO_URL"])); s=s.replace("${GITOPS_REVISION}",json.dumps(os.environ["GITOPS_REVISION"])); sys.stdout.write(s)' \
  | kubectl apply -f -
kubectl -n argocd wait application/mlops-gitops-smoke \
  --for=jsonpath='{.status.sync.status}'=Synced --timeout=5m
[[ "$(kubectl get configmap argocd-gitops-smoke -o jsonpath='{.data.result}')" == synced-from-git ]] || {
  echo "Argo CD did not sync the expected ConfigMap" >&2; exit 1;
}
echo "GitOps sample synced from $GITOPS_REPO_URL at $GITOPS_REVISION"
