#!/usr/bin/env bash
#
# Bring up the whole demo: k3d cluster, storage, observability stack, and
# Argo Workflows wired to emit OpenTelemetry traces.
#
# Component versions live next to the thing that installs them:
#   k3s                    k3d.conf
#   argo-workflows         kustomization.yaml + argo-workflow-controller-cm.yaml
#   otel collector         opentelemetry-collector.yaml
#   otel operator          opentelemetry-operator.yaml (vanilla upstream)
#   jaeger                 jaeger.yaml
#   cert-manager / tempo / kube-prometheus-stack   below

set -euo pipefail

CERT_MANAGER_VERSION=v1.21.2
TEMPO_CHART_VERSION=1.24.4
KPS_CHART_VERSION=91.4.1

cd "$(dirname "$0")"

# Use a dedicated kubeconfig rather than inheriting whatever KUBECONFIG points
# at. A multi-entry KUBECONFIG stops k3d writing the new context at all, which
# would otherwise leave every kubectl below aimed at an unrelated cluster.
KUBECONFIG_FILE="${KUBECONFIG_FILE:-$HOME/.kube/configs/k3d-otel.yaml}"
mkdir -p "$(dirname "$KUBECONFIG_FILE")"
export KUBECONFIG="$KUBECONFIG_FILE"

if k3d cluster list otel >/dev/null 2>&1; then
	echo "k3d cluster 'otel' already exists. Remove it first:" >&2
	echo "    k3d cluster delete otel" >&2
	exit 1
fi

helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update grafana prometheus-community >/dev/null

k3d cluster create --config k3d.conf

# Refuse to touch anything unless we are pointed at the cluster we just made.
context="$(kubectl config current-context)"
if [[ "$context" != "k3d-otel" ]]; then
	echo "Expected kubectl context 'k3d-otel', got '$context'. Refusing to continue." >&2
	exit 1
fi

# Taint the controlplane so that nothing runs on it
kubectl taint node k3d-otel-server-0 k3s-controlplane=true:NoSchedule

for ns in argo minio tempo jaeger monitoring warehouse; do
	kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

kubectl apply -f minio-deploy.yaml
kubectl apply -f jaeger.yaml

# Postgres for the data-lineage sample. The secret goes in both namespaces
# because the pipeline pods run in `default`.
kubectl apply -n warehouse -f warehouse-secret.yaml
kubectl apply -n default -f warehouse-secret.yaml
kubectl apply -f warehouse.yaml

kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
kubectl rollout status deployment cert-manager-webhook -n cert-manager --timeout=300s

# Vanilla upstream release. The locally patched build this repo used to carry is
# no longer needed: init container instrumentation landed upstream in v0.146.0.
#   wget https://github.com/open-telemetry/opentelemetry-operator/releases/download/v0.159.0/opentelemetry-operator.yaml
kubectl apply --server-side -f opentelemetry-operator.yaml
kubectl rollout status deployment opentelemetry-operator-controller-manager \
	-n opentelemetry-operator-system --timeout=300s

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
	-n monitoring --version "${KPS_CHART_VERSION}" --values kube-prometheus-values.yaml --wait --timeout 10m

helm upgrade --install tempo grafana/tempo \
	-n tempo --version "${TEMPO_CHART_VERSION}" --values tempo-values.yaml --wait --timeout 5m

kubectl apply -n default -f minio-secret.yaml
kubectl apply -n argo -f minio-secret.yaml

# These must exist BEFORE any pod that wants SDK injection is admitted. kubectl
# orders a single apply by kind and creates Deployments before custom resources,
# so leaving these inside the kustomization makes the pod mutating webhook fail
# with "no OpenTelemetry Instrumentation instances available" on a fresh cluster.
kubectl apply -f opentelemetry-instrumentation.yaml
kubectl apply -f opentelemetry-instrumentation-default.yaml

# The collector CR needs the operator's CRDs and webhook, both ready by now.
kubectl apply --server-side -k .
kubectl apply -f role.yaml
kubectl apply -f rolebinding.yaml

kubectl rollout status deployment workflow-controller -n argo --timeout=300s
kubectl rollout status deployment argo-server -n argo --timeout=300s
kubectl rollout status deployment workflows-collector -n argo --timeout=300s
kubectl rollout status deployment jaeger -n jaeger --timeout=300s
kubectl rollout status deployment warehouse -n warehouse --timeout=300s

cat <<'MSG'

Deployed. The k3d loadbalancer maps these to localhost:

  Jaeger          http://localhost:16686
  Grafana         http://localhost:3000        (admin/admin)
  Argo Server     https://localhost:2746       (token from ./rwwf.sh)
  MinIO console   http://localhost:9001        (admin/password)

MSG
cat <<MSG
The cluster's kubeconfig was written to:

  $KUBECONFIG_FILE

Context name is k3d-otel. To use it from your shell:

  export KUBECONFIG=$KUBECONFIG_FILE

Submit a traced workflow:

  kubectl create -n default -f otel-cli-trace.yaml

The data-lineage sample needs its image built first:

  ./build-datasci.sh
  kubectl create -n default -f datasci-lineage-trace.yaml

MSG
