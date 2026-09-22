"""Derive data lineage by reading the workflow's own trace back out of Jaeger.

Nothing here parses the pipeline source. The SQL is recovered from span
attributes that the auto-instrumented psycopg client produced, so this would work
just as well against a pipeline written in another language, or one whose source
we have never seen.

    python lineage.py [out_dir]
"""

import html
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import sqlglot
from sqlglot import exp

JAEGER = os.environ.get("JAEGER_URL", "http://jaeger.jaeger.svc.cluster.local:16686")
# Attribute names for the SQL text: db.statement is what the current
# instrumentation emits by default, db.query.text is the newer stable semconv
# name used when OTEL_SEMCONV_STABILITY_OPT_IN=database.
SQL_KEYS = ("db.statement", "db.query.text")
MIN_STATEMENTS = int(os.environ.get("MIN_STATEMENTS", "4"))


def trace_id() -> str:
    """This step runs inside the very trace it analyses, so its own TRACEPARENT
    carries the trace id. Format: 00-<32 hex trace>-<16 hex span>-<flags>."""
    traceparent = os.environ.get("TRACEPARENT", "")
    parts = traceparent.split("-")
    if len(parts) < 3 or len(parts[1]) != 32:
        sys.exit(f"no usable TRACEPARENT in environment (got {traceparent!r})")
    return parts[1]


def fetch_trace(tid: str, timeout: int = 90) -> tuple[list[dict], dict]:
    """Poll until the DB spans have landed; return (spans, processes).

    Spans from the earlier steps are exported when each Python process exits, and
    the collector does not batch traces, so they arrive quickly - but not
    necessarily before this step starts.
    """
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{JAEGER}/api/traces/{tid}", timeout=10) as r:
                trace = (json.load(r).get("data") or [{}])[0]
            spans = trace.get("spans", [])
            seen = sum(1 for s in spans if statement_of(s))
            if seen >= MIN_STATEMENTS:
                return spans, trace.get("processes", {})
            last = f"{seen} of {MIN_STATEMENTS} SQL spans so far"
        except Exception as err:  # transient while spans are still in flight
            last = f"{type(err).__name__}: {err}"
        time.sleep(3)
    sys.exit(f"gave up waiting for SQL spans in trace {tid} ({last})")


def statement_of(span: dict) -> str | None:
    for tag in span.get("tags", []):
        if tag.get("key") in SQL_KEYS and str(tag.get("value", "")).strip():
            return str(tag["value"])
    return None


def stage_of(span: dict, processes: dict) -> str:
    """The pipeline sets OTEL_SERVICE_NAME per step, so the span's service name is
    the stage name. Without that the operator would default it to the pod name,
    which embeds the shared Argo template name rather than the step name."""
    return processes.get(span.get("processID", ""), {}).get("serviceName", "?")


def tables(node: exp.Expression) -> list[str]:
    out = []
    for table in node.find_all(exp.Table):
        name = table.name
        if name and name not in out:
            out.append(name)
    return out


def analyse(statement: str) -> tuple[str | None, list[str]]:
    """Return (table written, tables read) for one statement.

    Anything that neither writes nor reads a table - DROP, CREATE INDEX, SET,
    COMMIT and the like - comes back as (None, []) and is ignored by the caller.
    """
    try:
        parsed = sqlglot.parse_one(statement, dialect="postgres")
    except Exception:
        return None, []

    if isinstance(parsed, exp.Create):
        if (parsed.args.get("kind") or "").upper() != "TABLE":
            return None, []
        target = parsed.this.name if isinstance(parsed.this, exp.Table) else None
        body = parsed.expression  # the SELECT of a CREATE TABLE ... AS SELECT
        return (target, tables(body)) if body is not None else (None, [])

    if isinstance(parsed, (exp.Insert, exp.Update, exp.Delete)):
        this = parsed.this
        # INSERT INTO t (a, b) SELECT ... wraps the target table in a Schema node
        # naming the columns; INSERT INTO t SELECT ... does not.
        if isinstance(this, exp.Schema):
            this = this.this
        target = this.name if isinstance(this, exp.Table) else None
        reads = [t for t in tables(parsed) if t != target]
        return target, reads

    if isinstance(parsed, exp.Select):
        return None, tables(parsed)

    return None, []


def main() -> None:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/lineage"
    os.makedirs(out_dir, exist_ok=True)

    tid = trace_id()
    print(f"analysing trace {tid}\n")
    spans, processes = fetch_trace(tid)

    # Pass 1: pull (stage, target, sources) out of every span carrying SQL.
    parsed: list[tuple[str, str | None, list[str]]] = []
    per_stage: dict[str, dict[str, list[str]]] = {}

    for span in spans:
        statement = statement_of(span)
        if not statement:
            continue
        stage = stage_of(span, processes)
        target, sources = analyse(statement)
        if not target and not sources:
            continue
        parsed.append((stage, target, sources))
        bucket = per_stage.setdefault(stage, {"writes": [], "reads": []})
        if target and target not in bucket["writes"]:
            bucket["writes"].append(target)

    # Pass 2: build the graph. Done separately because whether a read counts as
    # lineage depends on what its stage wrote, and Jaeger does not return spans
    # in a guaranteed order.
    edges: list[tuple[str, str, str]] = []   # (source table, target table, stage)
    reads_only: list[tuple[str, str]] = []   # (stage, source table)

    for stage, target, sources in parsed:
        bucket = per_stage[stage]
        for source in sources:
            if source not in bucket["reads"] and source not in bucket["writes"]:
                bucket["reads"].append(source)
        if target:
            for source in sources:
                if (source, target, stage) not in edges:
                    edges.append((source, target, stage))
        else:
            for source in sources:
                # Reading back a table this same stage just wrote (the row-count
                # check after a CREATE TABLE AS) is validation, not lineage.
                if source in bucket["writes"]:
                    continue
                if (stage, source) not in reads_only:
                    reads_only.append((stage, source))

    if not edges:
        sys.exit("no lineage edges recovered - check that spans carry db.statement")

    print("stages observed (from span service names):")
    for stage in sorted(per_stage):
        b = per_stage[stage]
        print(f"  {stage:<16} reads={b['reads'] or '-'} writes={b['writes'] or '-'}")

    derived = {target for _, target, _ in edges}
    sources = {source for source, _, _ in edges} - derived

    # Most stages write a table of the same name, which would make every edge
    # label a copy of the box it points at. Only label an edge where the stage
    # name actually adds something.
    def label(stage: str, target: str) -> str:
        return "" if stage == target else stage

    mermaid = ["graph LR"]
    for table in sorted(sources):
        mermaid.append(f"  {table}[({table})]")
    for table in sorted(derived):
        mermaid.append(f"  {table}[{table}]")
    for source, target, stage in edges:
        tag = label(stage, target)
        mermaid.append(f"  {source} -->|{tag}| {target}" if tag
                       else f"  {source} --> {target}")
    for stage, source in reads_only:
        mermaid.append(f"  {source} -.->|reads| {stage}_out[/{stage}/]")

    dot = ["digraph lineage {", '  rankdir=LR;', '  node [shape=box];']
    for table in sorted(sources):
        dot.append(f'  "{table}" [shape=cylinder];')
    for source, target, stage in edges:
        tag = label(stage, target)
        dot.append(f'  "{source}" -> "{target}" [label="{tag}"];' if tag
                   else f'  "{source}" -> "{target}";')
    for stage, source in reads_only:
        dot.append(f'  "{source}" -> "{stage}" [style=dashed,label="reads"];')
    dot.append("}")

    print("\n" + "\n".join(mermaid))

    dot_text = "\n".join(dot) + "\n"
    mermaid_text = "\n".join(mermaid) + "\n"
    write(out_dir, "lineage.dot", dot_text)
    write(out_dir, "lineage.mmd", mermaid_text)

    svg = render(dot_text, "svg", os.path.join(out_dir, "lineage.svg"))
    render(dot_text, "png", os.path.join(out_dir, "lineage.png"))

    write(out_dir, "lineage.html",
          report(tid, per_stage, sorted(sources), sorted(derived), svg, mermaid_text))


def write(out_dir: str, name: str, body: str) -> None:
    path = os.path.join(out_dir, name)
    with open(path, "w") as handle:
        handle.write(body)
    print(f"wrote {path}")


def render(dot_text: str, fmt: str, path: str) -> str:
    """Render the DOT with graphviz. Returns the output as text for svg, else "".

    Missing graphviz is not fatal: the HTML report falls back to showing the
    Mermaid source, so the lineage is still readable.
    """
    if not shutil.which("dot"):
        print(f"graphviz not installed, skipping {fmt}")
        return ""
    try:
        # -o writes the image, so stdout stays empty and text mode is safe for
        # both formats; the SVG is read back off disk.
        subprocess.run(["dot", f"-T{fmt}", "-o", path], input=dot_text,
                       text=True, capture_output=True, check=True)
        print(f"wrote {path}")
        return open(path).read() if fmt == "svg" else ""
    except (subprocess.CalledProcessError, OSError) as err:
        print(f"graphviz failed for {fmt}: {err}")
        return ""


def inline_svg(svg: str) -> str:
    """Strip graphviz's XML prolog and DOCTYPE so the <svg> can be embedded in
    HTML. It has to be inline markup rather than an <img src>, because the Argo
    artifact server serves artifacts under a strict Content-Security-Policy
    (default-src 'none') that blocks subresource fetches. Inline markup is not a
    fetch, so it renders."""
    start = svg.find("<svg")
    return svg[start:] if start != -1 else ""


def report(tid, per_stage, sources, derived, svg, mermaid_text) -> str:
    rows = []
    for stage in sorted(per_stage):
        bucket = per_stage[stage]
        rows.append(
            "<tr><td class=s>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(stage),
                ", ".join(html.escape(t) for t in bucket["reads"]) or "&mdash;",
                ", ".join(html.escape(t) for t in bucket["writes"]) or "&mdash;",
            )
        )

    graph = inline_svg(svg)
    if graph:
        figure = f'<div class=graph>{graph}</div>'
    else:
        figure = ("<p class=note>graphviz unavailable; Mermaid source below.</p>"
                  f"<pre>{html.escape(mermaid_text)}</pre>")

    # No scripts and no external resources: the artifact CSP forbids both.
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Data lineage</title>
<style>
  body {{ font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 24px;
         color: #1a1a1a; background: #fff; }}
  h1 {{ font-size: 19px; margin: 0 0 4px; }}
  h2 {{ font-size: 15px; margin: 28px 0 8px; }}
  .meta {{ color: #666; font-size: 12px; margin: 0 0 4px; font-family: ui-monospace, monospace; }}
  .lede {{ color: #444; max-width: 62em; }}
  table {{ border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 5px 14px 5px 0; border-bottom: 1px solid #eee;
            vertical-align: top; }}
  th {{ color: #666; font-weight: 600; }}
  td.s {{ font-family: ui-monospace, monospace; }}
  .graph {{ overflow: auto; border: 1px solid #eee; padding: 12px; }}
  .graph svg {{ max-width: 100%; height: auto; }}
  pre {{ background: #f6f6f6; padding: 12px; overflow: auto; font-size: 12px; }}
  .tables {{ font-family: ui-monospace, monospace; font-size: 13px; }}
  .note {{ color: #a00; }}
</style></head><body>
<h1>Data lineage</h1>
<p class=meta>trace {html.escape(tid)}</p>
<p class=lede>Derived from the SQL captured in this workflow's trace spans, not from
the pipeline source. Cylinders are source tables, boxes are tables the pipeline
built. A dashed edge is a stage that read a table without writing anything. Edges
carry the stage name only where it differs from the table it produced; the table
below has the full stage-by-stage breakdown.</p>
{figure}
<h2>Per stage</h2>
<table><tr><th>stage</th><th>reads</th><th>writes</th></tr>
{chr(10).join(rows)}
</table>
<h2>Tables</h2>
<p class=tables><strong>source:</strong> {", ".join(html.escape(t) for t in sources) or "&mdash;"}<br>
<strong>derived:</strong> {", ".join(html.escape(t) for t in derived) or "&mdash;"}</p>
<h2>Mermaid</h2>
<pre>{html.escape(mermaid_text)}</pre>
</body></html>
"""


if __name__ == "__main__":
    main()
