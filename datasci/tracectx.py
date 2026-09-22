"""Platform-provided trace-context bridge.

This is NOT part of the data scientist's code. It exists because of a gap in the
OpenTelemetry Python auto-instrumentation the OTel operator injects:

  * the operator injects the SDK and the psycopg instrumentation, so database
    calls become spans with no code changes, but
  * auto-instrumentation never establishes a trace context from the environment.
    Its sitecustomize.py only calls initialize(), which loads the distro,
    configurators and instrumentors and nothing else.

So without this shim the pipeline's database spans would start a brand new trace
instead of nesting under the workflow's runMainContainer span, and the lineage
analysis would have no single trace to read.

The Argo workflow-controller injects a W3C TRACEPARENT environment variable into
every container of a workload pod. EnvironmentGetter normalises propagator keys
to upper case ("traceparent" -> "TRACEPARENT"), which is the same environment
carrier convention Argo implements on the Go side in util/telemetry/carrier.go.

Usage:
    python -m tracectx /app/pipeline.py daily_revenue

The wrapped script sees a normal sys.argv and runs as __main__, so it needs no
knowledge of any of this.
"""

import os
import runpy
import sys

from opentelemetry import context
from opentelemetry.propagate import extract

try:
    # Private API (underscore-prefixed) but shipped in opentelemetry-api and
    # documented for exactly this case: "usually os.environ during application
    # or child-process initialization". Fall back to a plain dict carrier if it
    # ever moves, so this shim cannot be what breaks the pipeline.
    from opentelemetry.propagators._envcarrier import EnvironmentGetter

    _carrier, _getter = os.environ, EnvironmentGetter()
except ImportError:  # pragma: no cover
    _carrier, _getter = {
        key.lower(): value
        for key, value in os.environ.items()
        if key in ("TRACEPARENT", "TRACESTATE", "BAGGAGE")
    }, None


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python -m tracectx <script.py> [args...]")

    parent = extract(_carrier, getter=_getter) if _getter else extract(_carrier)
    context.attach(parent)

    script, sys.argv = sys.argv[1], sys.argv[1:]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
