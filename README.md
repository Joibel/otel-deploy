# OTEL Deploy

Build your three images from the tracing base branch.

make argoexec-image
make workflow-controller-image
make argocli-image

In theory these make targets can push the results into k3d for you, I didn't do that because my cluster is called otel - I pushed to docker instead

DOCKER_PUSH=true
IMAGE_NAMESPACE=pipekitdev

## Starting stuff

Take a deep breath and run deploy.sh

This deploys
* a k3d cluster
* minio as blob storage for artifacts
* argo-workflows (you'll need to patch the image locations for your own arm images - 3 of them). Search `pipekitdev` to find them
* Grafana Tempo for trace collection
* Prometheus stack (including grafana) for metrics and grafana
* cert-manager (pre-req for otel operator)
* OpenTelemetry operator - patch this back to one you can run - my version is for init container patching
* Roles and rolebindings to run stuff in namespace `default` - this is where you should shove workflows

## Seeing stuff

Portforward
* Grafana: port 3000
* argo-server: port 2746

./rwwf.sh will give you a bearer token
Then you can visit localhost:2746 and login to workflows.

Visit grafana on localhost:3000, login as admin/admin and change the password to admin again to make it shutup.
Go to Data Sources -> Add New Data Source -> Tempo. Change the connection URL to http://tempo.tempo.svc.cluster.local:3100 and Save & Test.

`kubectl create -f dag-diamond-trace.yaml` from this repo. If it all works you'll get a running workflow, and a trace button at the top of the page. Go to that and you might see a trace.
