# OTEL Deploy

A local Kubernetes environment for demoing OpenTelemetry tracing of Argo
Workflows. Traces go to **both Jaeger and Grafana Tempo**, and span metrics go to
Prometheus.

Everything runs released upstream images. There is nothing to build.

## Prerequisites

`k3d`, `kubectl`, `helm` and a working Docker daemon. `deploy.sh` adds the
`grafana` and `prometheus-community` Helm repos itself.

## Starting stuff

```bash
./deploy.sh
```

This deploys

* a k3d cluster (1 server + 3 agents, k3s v1.37.0)
* MinIO as blob storage for artifacts
* Argo Workflows v4.1.3 — released images, tracing enabled
* Jaeger v2 all-in-one, memory storage
* Grafana Tempo for trace storage
* kube-prometheus-stack (Prometheus + Grafana + Alertmanager)
* cert-manager (pre-req for the OTel operator)
* the OpenTelemetry operator and a collector
* roles and rolebindings to run workflows in namespace `default`

## Seeing stuff

The k3d loadbalancer forwards host ports to NodePort services, so there is no
`kubectl port-forward` to keep alive:

| URL | What | Login |
|-----|------|-------|
| <http://localhost:16686> | Jaeger | none |
| <http://localhost:3000> | Grafana | admin/admin |
| <https://localhost:2746> | Argo Server | bearer token from `./rwwf.sh` |
| <http://localhost:9001> | MinIO console | admin/password |

Grafana has **Tempo** (uid `tempo`) and **Jaeger** (uid `jaeger`) datasources
provisioned already — no manual datasource setup needed.

## Proving traces work

```bash
kubectl create -n default -f otel-cli-trace.yaml
```

Then either:

* open the workflow in the Argo UI and use the **Trace in Jaeger** or
  **Trace in Grafana Tempo** button at the top of the page, or the
  **Pod trace in Jaeger** button on an individual pod, which opens the
  workflow trace with that pod's spans highlighted, or
* search for service `workflow-controller` in Jaeger at
  <http://localhost:16686>.

A trace contains spans from three sources, all stitched into one tree:

* **the controller** — `workflow`, `node`, `reconcile_workflow`,
  `create_workflow_pod`, `persist_updates`, …
* **the executor (pod traces)** — `runInitContainer`, `runMainContainer`,
  `runWaitContainer`, `saveArtifacts`, `saveLogs`, `createTaskResult`, …
  The controller stamps a W3C `TRACEPARENT` env var into every container of the
  workload pod, which is how argoexec joins the workflow's trace.
* **your workload** — `otel-cli-trace.yaml` runs `otel-cli`, which reads the
  `TRACEPARENT` the emissary exports, so `observability`/`summit`/`prague` appear nested
  under `runMainContainer`.

The controller also annotates the Workflow, and each pod, with
`workflows.argoproj.io/trace-id` and (pods only)
`workflows.argoproj.io/span-id`:

```bash
kubectl get wf -n default -o custom-columns=\
NAME:.metadata.name,TRACE:'.metadata.annotations.workflows\.argoproj\.io/trace-id'
```

`artifact-trace.yaml` is a second sample that passes an artifact through MinIO,
which adds `saveArtifacts`/`archiveArtifact` and
`loadArtifacts`/`unarchiveArtifact` spans to the tree.

`buildkit-trace.yaml` is the most interesting one. It clones this repo, then
builds a small multi-stage image with BuildKit and pushes it to the k3d registry.
BuildKit emits its own OpenTelemetry spans and picks up `TRACEPARENT` from the
executor, so the Dockerfile's own steps appear nested inside the workflow trace.

Two things are worth looking at, and they are opposites.

**The build step** has two builder stages rooted in different base images, with
no dependency between them, so BuildKit resolves and pulls all three images at
once and the branches only meet at the two `COPY --from` lines:

```
t+13.5s  1.3s  resolving gcr.io/distroless/static-debian13   } all three
t+13.5s  1.3s  resolving docker.io/library/node:lts-alpine   } start on the
t+13.5s  1.3s  resolving docker.io/library/golang:1.27.1     } same tick

[gobuild  2/4] WORKDIR  |  [webbuild 2/4] WORKDIR    in lockstep
[gobuild  4/4] RUN go build  |  [webbuild 4/4] RUN node build.js
[stage-2 2/3] COPY --from=gobuild  ... branches rejoin
```

**The clone step** is the opposite: git is not OpenTelemetry-aware, so that pod
produces only the spans argoexec wraps around it, with nothing at all nested
inside `runMainContainer`. Side by side in one trace they show exactly what
instrumenting a workload does and does not buy you:

```
runMainContainer [clone]  nested spans = 0
runMainContainer [build]  nested spans = 250
```

About 380 spans and 50 seconds, of which the clone step costs only 19 spans. The
clone is load-bearing rather than decoration: it passes the repo's commit id to
the build as an artifact, and the Dockerfile stamps it into the image.

Be aware when passing artifacts into a rootless builder that Argo stages them as
`0600` owned by root while rootless BuildKit runs as uid 1000, so it cannot read
them. The symptom is silent: an empty file in the image and a successful build.
The `mode: 0644` on that input artifact is what prevents it.

`buildkit-controller-trace.yaml` is a heavier variant kept for interest. It
builds the real Argo workflow-controller from source: more realistic, but about
470 spans and two minutes, with one 65-second Go compile on one branch and a
trivial one on the other, so the parallel structure is harder to see.

## Data lineage from traces

`datasci-lineage-trace.yaml` simulates a data scientist's pipeline and then
recovers its **data lineage from the trace**, not from the source.

```bash
./build-datasci.sh                                        # once
kubectl create -n default -f datasci-lineage-trace.yaml
```

`datasci/pipeline.py` is ordinary `psycopg` code against the Postgres in
`warehouse.yaml`, with **no OpenTelemetry imports at all**. The OTel operator
auto-instruments the container, so every statement becomes a span carrying the SQL
in `db.statement`. The final step reads the trace back out of Jaeger, parses those
statements with `sqlglot`, and emits the graph - it never reads the pipeline
source, so it would work against a pipeline in any language:

```
customers ──┐
orders ─────┼──> customer_ltv ──┐
order_items ┘                   ├──> exec_summary ⇢ report
orders ─────┐                   │
            ├──> daily_revenue ─┘
order_items ┘
```

Note what is *absent*: the schema also has a `products` table, and it never
appears, because nothing in the pipeline reads it. Lineage from traces tells you
what the pipeline actually touched, not what the schema contains.

The lineage step emits two artifacts you can open directly in the Argo UI, on the
**Artifacts** tab of the `lineage` node:

* **`lineage-report`** - a self-contained HTML report: the graph, a per-stage
  reads/writes table, and the Mermaid source
* **`lineage-graph`** - the same graph as a PNG

The Argo UI only auto-renders artifacts whose extension is one of
`gif/jpg/jpeg/json/html/png/txt`, and refuses anything it treats as a tgz - which
includes any directory artifact, since Argo tars those regardless of
`archive: none`. So these are single files, each with `archive: none`. The report
is HTML with the graph inlined as `<svg>` markup rather than an `<img>`, because
the artifact server serves artifacts under a strict CSP (`default-src 'none'`, no
scripts) that blocks subresource fetches and scripts; inline markup is neither, so
it renders. That also rules out a Mermaid-from-CDN page, which is why the graph is
pre-rendered with graphviz inside the image.

### The two things this needed

* **A Python endpoint on 4318.** The operator's injected Python distro bundles
  only `opentelemetry-exporter-otlp-proto-http` - no gRPC exporter, deliberately -
  and forces `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. So
  `opentelemetry-collector.yaml` has an HTTP receiver alongside the gRPC one, and
  `opentelemetry-instrumentation-default.yaml` points Python at 4318 via
  `spec.python.env` while Go keeps 4317.
* **`datasci/tracectx.py`.** Auto-instrumentation never establishes trace context
  from the environment: its `sitecustomize.py` only calls `initialize()`. Without
  a bridge the pipeline's spans would start their own trace and there would be no
  single trace to analyse. This ~10-line platform-owned shim uses
  `opentelemetry.propagators._envcarrier.EnvironmentGetter` (shipped in
  opentelemetry-api, and the same environment-carrier convention Argo uses in
  `util/telemetry/carrier.go`) to adopt the `TRACEPARENT` the controller injected.
  Run as `python -m tracectx pipeline.py <stage>`; the pipeline stays OTel-free.

Each step also sets `OTEL_SERVICE_NAME` to its stage name. The operator only
defaults that when unset, and without it every pod would be named after the shared
`stage` template, leaving the analyser unable to tell the stages apart.

Span metrics are also derived from the traces by the collector's `spanmetrics`
connector and pushed to Prometheus over remote write. The `prometheusremotewrite`
exporter sanitises metric names, so they arrive as `span_metrics_*` (underscores)
even though the connector namespace is `span.metrics`:

```promql
span_metrics_calls_total
span_metrics_duration_milliseconds_bucket
```

## How tracing is configured

Argo turns tracing on as soon as `OTEL_EXPORTER_OTLP_ENDPOINT` is set — there is
no separate enable flag. That env var is injected by the OpenTelemetry operator:

* `argo-workflow-controller-deployment.yaml` carries
  `instrumentation.opentelemetry.io/inject-sdk` for the controller
* `argo-workflow-controller-cm.yaml` sets `workflowDefaults.podMetadata` so every
  workload pod gets the same annotation for its `init`, `wait` and `main`
  containers
* `opentelemetry-instrumentation.yaml` / `-default.yaml` are the `Instrumentation`
  CRs naming the collector endpoint, one per namespace that needs it

Instrumenting **init containers** used to need a patched operator build; that
landed upstream in operator v0.146.0, so `opentelemetry-operator.yaml` is now a
verbatim upstream release.

The collector (`opentelemetry-collector.yaml`) receives OTLP on 4317 and fans
traces out to Tempo, Jaeger and the `spanmetrics` connector.

## Notes

* Jaeger uses in-memory storage, so traces are lost if its pod restarts.
* Grafana's **Jaeger** datasource can look a trace up by ID (which is what the
  Argo UI links do) but its "Search" tab service dropdown stays empty: it calls
  `/api/services`, which Jaeger v2 dropped in favour of `/api/v3/services`. Use
  the Jaeger UI itself for searching, or the Tempo datasource in Grafana.
* The `grafana/tempo` single-binary chart now prints a deprecation warning on
  install. It still works; `tempo-distributed` is the maintained replacement if
  this ever needs to outlive the demo.
* Tempo is pinned by chart version, and chart 1.24.4 ships Tempo 2.9.0 rather
  than the newest 3.x, whose config schema the chart does not yet target.
* Workflows should be submitted to namespace `default`, which has the roles and
  the `Instrumentation` CR it needs.
* The k3d registry has three names and the right one depends on who is asking:
  `localhost:5000` from the host; **`k3d-registry.localhost:5000` in image
  references**, because the node's `registries.yaml` has a mirror for it and so
  containerd pulls it over plain HTTP; and **`host.k3d.internal:5000` from code
  running inside a pod** (BuildKit pushing, for instance), because client
  libraries shortcut the `.localhost` TLD to loopback per RFC 6761. They are one
  registry, so an image pushed under one name pulls under another.
* Postgres in `warehouse.yaml` uses an `emptyDir`, so restarting the pod re-runs
  the seed. That keeps the demo self-contained and idempotent; it is not a
  durable database.
* Viewing artifacts in the UI needs the logged-in user to be able to read the
  artifact repository credentials, because the artifact server fetches them as
  that user. `rw-user.yaml` therefore grants `get` on the `my-minio-cred` secret;
  without it the Artifacts panel returns a 500 and the argo-server logs
  `secrets "my-minio-cred" is forbidden`.
* `datasci-lineage-trace.yaml` sets `imagePullPolicy: Always`, because the image
  tag is fixed and the default `IfNotPresent` would otherwise keep a stale cached
  image after `./build-datasci.sh` rebuilds it.
