#!/usr/bin/env bash
# Requires kubectl and Helm installed from official, verified releases.
set -euo pipefail
export AWS_PAGER=''
root=$(cd "$(dirname "$0")/.." && pwd)
test "$(aws sts get-caller-identity --query Account --output text)" = 521595303406
aws eks update-kubeconfig --region ap-south-1 --name market-intel-prod
kubectl get nodes
helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm repo update argo
helm upgrade --install argocd argo/argo-cd --version 10.10.5 \
  --namespace argocd --create-namespace --values "$root/deploy/argocd/values.yaml" \
  --wait --timeout 10m
kubectl apply -f "$root/deploy/namespace.yaml"
kubectl apply -f "$root/deploy/argocd/project.yaml"
kubectl apply -f "$root/deploy/argocd/application.yaml"
kubectl -n argocd get application market-intel
