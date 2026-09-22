#!/usr/bin/env bash
#
# Print a bearer token for logging in to the Argo Server UI.

set -euo pipefail

cd "$(dirname "$0")"

# Same dedicated kubeconfig deploy.sh writes, so this cannot land on whichever
# cluster an inherited multi-entry KUBECONFIG happens to resolve to.
export KUBECONFIG="${KUBECONFIG_FILE:-$HOME/.kube/configs/k3d-otel.yaml}"

context="$(kubectl config current-context)"
if [[ "$context" != "k3d-otel" ]]; then
	echo "Expected kubectl context 'k3d-otel', got '$context'. Refusing to continue." >&2
	exit 1
fi

kubectl apply -f rw-user.yaml

# The token is populated asynchronously by the service account token controller.
for _ in $(seq 1 30); do
	token="$(kubectl get secret -n argo argo-workflows-rw-user -o jsonpath='{.data.token}' 2>/dev/null || true)"
	[[ -n "$token" ]] && break
	sleep 1
done

if [[ -z "${token:-}" ]]; then
	echo "Timed out waiting for the argo-workflows-rw-user token." >&2
	exit 1
fi

echo "Bearer $(printf '%s' "$token" | base64 --decode)"
