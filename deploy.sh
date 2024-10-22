#!/usr/bin/env bash

set -eux
k3d cluster create --config k3d.conf
#Taint the controlplane so that nothing runs on it
kubectl taint node k3d-otel-server-0 k3s-controlplane=true:NoSchedule

kubectl create namespace argo || true
kubectl create namespace minio || true
kubectl create namespace tempo || true

kubectl apply -f minio-deploy.yaml
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.0/cert-manager.yaml
while ! kubectl get deployment cert-manager-webhook -n cert-manager; do echo "Waiting for cert manager..."; sleep 1; done
kubectl rollout status deployment cert-manager-webhook -n cert-manager --timeout=300s
# wget https://github.com/open-telemetry/opentelemetry-operator/releases/download/v0.109.0/opentelemetry-operator.yaml
kubectl apply -f opentelemetry-operator.yaml
while ! kubectl get deployment opentelemetry-operator-controller-manager -n opentelemetry-operator-system; do echo "Waiting for otel operator..."; sleep 1; done
kubectl rollout status deployment opentelemetry-operator-controller-manager -n opentelemetry-operator-system --timeout=120s

kubectl apply --server-side -f kube-prometheus/manifests/setup
kubectl wait \
	--for condition=Established \
	--all CustomResourceDefinition \
	--namespace=monitoring
kubectl apply -f kube-prometheus/manifests/

helm install tempo grafana/tempo -n tempo --values tempo-values.yaml || true

kubectl apply -n default -f minio-secret.yaml
kubectl apply -n argo -f minio-secret.yaml
kubectl apply -k .
kubectl apply -f role.yaml
kubectl apply -f rolebinding.yaml
kubectl apply -f opentelemetry-instrumentation-default.yaml
