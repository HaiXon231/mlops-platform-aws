#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/k8s-common.sh"
check_cluster

helm status karpenter-crd -n kube-system >/dev/null
helm status karpenter -n kube-system >/dev/null
helm status argocd -n argocd >/dev/null
kubectl -n kube-system rollout status deployment/karpenter --timeout=5m
kubectl -n argocd rollout status deployment/argocd-server --timeout=5m
kubectl -n argocd rollout status deployment/argocd-repo-server --timeout=5m
kubectl -n argocd rollout status deployment/argocd-applicationset-controller --timeout=5m
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=5m
kubectl wait --for=condition=Ready ec2nodeclass/mlops-aws-dev --timeout=5m
kubectl get ec2nodeclass mlops-aws-dev
kubectl get nodepools training-spot inference-on-demand
kubectl -n kube-system get pods -l app.kubernetes.io/name=karpenter -o wide
kubectl -n argocd get pods
