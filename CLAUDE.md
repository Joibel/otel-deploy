# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository deploys a local Kubernetes environment for tracing Argo Workflows
with OpenTelemetry, intended as a demo. It sets up a k3d cluster with:
- Argo Workflows v4.1.3 (released upstream images - nothing is built locally)
- Jaeger v2 all-in-one (memory storage) - primary trace UI
- Grafana Tempo (trace storage, queried from Grafana)
- kube-prometheus-stack via Helm (Prometheus + Grafana + Alertmanager)
- OpenTelemetry Operator (verbatim upstream release) and Collector
- MinIO (artifact storage)
- cert-manager

## Deployment Commands

```bash
# Full deployment - creates k3d cluster and deploys all components
./deploy.sh

# Get Argo Workflows bearer token for UI login
./rwwf.sh
```

## Architecture

**Cluster**: k3d cluster named "otel" with 1 server + 3 agents running k3s v1.37.0

**Python auto-instrumentation**: the operator's injected Python distro has no gRPC
exporter and forces `http/protobuf`, so the collector runs an HTTP receiver on 4318
and `opentelemetry-instrumentation-default.yaml` redirects Python there via
`spec.python.env`. Go components keep gRPC on 4317. Python auto-instrumentation
also does not adopt `TRACEPARENT` from the environment, which is what
`datasci/tracectx.py` exists to fix.

**Namespaces**:
- `argo`: Argo Workflows components and the OTel Collector
- `jaeger`: Jaeger all-in-one
- `tempo`: Grafana Tempo
- `monitoring`: kube-prometheus-stack
- `minio`: MinIO storage
- `warehouse`: Postgres for the data-lineage sample
- `default`: Where workflows should be submitted

**Trace Flow**:
1. Argo components (workflow-controller, argoexec in init/wait/main, and the
   workload itself) emit OTLP traces
2. OpenTelemetry Collector (`workflows` in argo namespace) receives traces on port 4317
3. Collector fans traces out to Jaeger, Tempo, and the spanmetrics connector
4. Span metrics are sent to Prometheus via remote write, arriving as
   `span_metrics_*` (the remote write exporter sanitises the `span.metrics`
   namespace to underscores)
5. Grafana queries Prometheus, Tempo (uid `tempo`) and Jaeger (uid `jaeger`)

**How tracing is enabled**: Argo starts tracing as soon as
`OTEL_EXPORTER_OTLP_ENDPOINT` is set; there is no separate flag. The OTel
operator injects it, driven by `instrumentation.opentelemetry.io/inject-sdk`
annotations on the controller Deployment and on `workflowDefaults.podMetadata`.
The controller injects a W3C `TRACEPARENT` env var into every container of a
workload pod, which is how argoexec and the user's workload join the trace.

**Key Files**:
- `kustomization.yaml`: Pulls the Argo release manifest and applies the patches below
- `argo-workflow-controller-deployment.yaml`: Controller patch (OTel annotation, requeue time)
- `argo-workflow-controller-cm.yaml`: Executor image, workflow defaults, artifact repo, UI trace links
- `argo-server-service.yaml`: Makes argo-server a NodePort
- `opentelemetry-collector.yaml`: Collector config - OTLP receiver, spanmetrics, Jaeger/Tempo/Prometheus exporters
- `jaeger.yaml`: Jaeger all-in-one Deployment and Services
- `kube-prometheus-values.yaml`: Helm values, including provisioned Grafana datasources
- `tempo-values.yaml`: Helm values for Tempo
- `k3d.conf`: k3d cluster configuration, including host port mappings
- `otel-cli-trace.yaml`, `artifact-trace.yaml`,
  `buildkit-trace.yaml`, `datasci-lineage-trace.yaml`: sample traced workflows
- `buildkit-controller-trace.yaml`: heavier BuildKit variant, kept for interest
- `warehouse.yaml`, `warehouse-secret.yaml`: Postgres for the data-lineage sample
- `datasci/`: pipeline (OTel-free), `tracectx.py` context bridge, `lineage.py` analyser
- `build-datasci.sh`: builds the datasci image to the k3d registry

## Exposed Services

The k3d loadbalancer maps host ports to NodePort services, so no port-forwarding
is needed:

| URL | Service | NodePort |
|-----|---------|----------|
| <http://localhost:16686> | Jaeger | 30686 |
| <http://localhost:3000> | Grafana (admin/admin) | 30300 |
| <https://localhost:2746> | Argo Server | 30746 |
| <http://localhost:9001> | MinIO console | 30901 |

## Updating versions

Chart and manifest versions are deliberately pinned, each next to what installs it:
- k3s: `k3d.conf`
- Argo Workflows: `kustomization.yaml` (release URL) and
  `argo-workflow-controller-cm.yaml` (`executor.image`) - keep these in step
- OTel Collector: `opentelemetry-collector.yaml` (`spec.image`)
- Jaeger: `jaeger.yaml`
- cert-manager, Tempo chart, kube-prometheus-stack chart: variables at the top of `deploy.sh`
- OTel Operator: re-download `opentelemetry-operator.yaml` from the upstream release

No images are built from source. Do not reintroduce the `pipekitdev`/`joibel`
tracing-branch images: tracing is in released Argo Workflows as of v4.1.0, and
the operator's init-container instrumentation landed upstream in v0.146.0.

# GitNexus — Code Intelligence

This project is indexed by GitNexus as **otel-deploy** (572 symbols, 588 relationships, 0 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/otel-deploy/context` | Codebase overview, check index freshness |
| `gitnexus://repo/otel-deploy/clusters` | All functional areas |
| `gitnexus://repo/otel-deploy/processes` | All execution flows |
| `gitnexus://repo/otel-deploy/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
