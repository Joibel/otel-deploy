#!/usr/bin/env bash
#
# Build and push the image used by datasci-lineage-trace.yaml.
#
# Pushed to localhost:5000, which Docker treats as insecure by default.
#
# The same registry has three names, and which one works depends on who is asking:
#
#   localhost:5000                from this host, via the published port
#   k3d-registry.localhost:5000   in image references, because containerd has a
#                                 mirror for it in the node's registries.yaml
#   host.k3d.internal:5000        from code running inside a pod (e.g. BuildKit
#                                 pushing), because client libraries shortcut the
#                                 .localhost TLD to loopback per RFC 6761
#
# They are the same registry, so an image pushed under one name pulls under another.

set -euo pipefail

cd "$(dirname "$0")"

TAG="${TAG:-1}"

if ! curl -sSf -o /dev/null http://localhost:5000/v2/ 2>/dev/null; then
	echo "k3d registry not reachable on localhost:5000 - is the cluster up?" >&2
	exit 1
fi

docker build -t "localhost:5000/datasci:${TAG}" datasci/
docker push "localhost:5000/datasci:${TAG}"

echo
echo "Pushed localhost:5000/datasci:${TAG}"
echo "In-cluster image reference: k3d-registry.localhost:5000/datasci:${TAG}"
