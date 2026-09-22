# Image for the data-lineage demo pipeline.
#
# Deliberately contains NO OpenTelemetry packages. The OTel operator injects its
# own SDK, exporter and instrumentation into an init container and puts them on
# PYTHONPATH; shipping our own copies here would risk version conflicts with the
# injected ones. psycopg and sqlglot are all the pipeline itself needs.
FROM python:3.14-slim

# graphviz renders the lineage graph. The Argo UI auto-renders artifacts whose
# extension is one of gif/jpg/jpeg/json/html/png/txt, so the report is HTML with
# the graph inlined as SVG, plus a PNG alongside it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends graphviz \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "psycopg[binary]==3.3.6" "sqlglot==30.18.0"

WORKDIR /app
COPY pipeline.py lineage.py tracectx.py /app/

# Unbuffered so step output interleaves correctly with the executor's logs.
ENV PYTHONUNBUFFERED=1
