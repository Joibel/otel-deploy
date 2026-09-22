"""Ordinary data pipeline. Plain psycopg, no OpenTelemetry anywhere.

Every statement run through psycopg becomes a span carrying the SQL, because the
OpenTelemetry operator auto-instruments this container. That is the whole point:
lineage is recovered from the traces afterwards (see lineage.py) without the
pipeline author doing anything to enable it.

    python pipeline.py <stage>
"""

import os
import sys

import psycopg

# Table definitions live in their own stage, the way a migration would in a real
# warehouse. The transform stages below then only INSERT, which is what an LTV or
# revenue table actually does - it accumulates, it is not dropped and rebuilt on
# every run.
#
# The lineage analysis ignores all of this: DDL names a table but moves no data
# between tables, so it contributes no edges.
SETUP = [
    "DROP TABLE IF EXISTS exec_summary",
    "DROP TABLE IF EXISTS daily_revenue",
    "DROP TABLE IF EXISTS customer_ltv",
    """
        CREATE TABLE daily_revenue (
          day         date          NOT NULL,
          order_count int           NOT NULL,
          revenue     numeric(12,2) NOT NULL
        )
    """,
    """
        CREATE TABLE customer_ltv (
          customer_id    int           NOT NULL,
          name           text          NOT NULL,
          country        text          NOT NULL,
          order_count    int           NOT NULL,
          lifetime_value numeric(12,2) NOT NULL
        )
    """,
    """
        CREATE TABLE exec_summary (
          total_revenue       numeric(14,2),
          active_days         int,
          best_customer_value numeric(12,2),
          best_customer       text
        )
    """,
]

# One statement per stage, so each stage is one span carrying one SQL statement.
# daily_revenue and customer_ltv read overlapping but different raw tables;
# exec_summary reads only the two derived tables. Note that `products` is never
# read by anything - the lineage graph should show that.
STAGES = {
    "daily_revenue": """
        INSERT INTO daily_revenue (day, order_count, revenue)
        SELECT o.ordered_at                     AS day,
               count(DISTINCT o.order_id)       AS order_count,
               sum(oi.quantity * oi.unit_price) AS revenue
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        WHERE o.status = 'complete'
        GROUP BY o.ordered_at
    """,
    "customer_ltv": """
        INSERT INTO customer_ltv (customer_id, name, country, order_count, lifetime_value)
        SELECT c.customer_id,
               c.name,
               c.country,
               count(DISTINCT o.order_id)       AS order_count,
               sum(oi.quantity * oi.unit_price) AS lifetime_value
        FROM customers c
        JOIN orders o       ON o.customer_id = c.customer_id
        JOIN order_items oi ON oi.order_id = o.order_id
        WHERE o.status = 'complete'
        GROUP BY c.customer_id, c.name, c.country
    """,
    "exec_summary": """
        INSERT INTO exec_summary (total_revenue, active_days, best_customer_value, best_customer)
        SELECT (SELECT sum(revenue)        FROM daily_revenue) AS total_revenue,
               (SELECT count(*)            FROM daily_revenue) AS active_days,
               (SELECT max(lifetime_value) FROM customer_ltv)  AS best_customer_value,
               (SELECT name FROM customer_ltv
                 ORDER BY lifetime_value DESC LIMIT 1)         AS best_customer
    """,
}

# A read-only stage, to show a leaf that consumes but produces nothing.
REPORT = "SELECT * FROM exec_summary"


def dsn() -> str:
    return (
        f"host={os.environ['PGHOST']} dbname={os.environ['PGDATABASE']} "
        f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']}"
    )


def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else ""

    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        if stage == "setup":
            for statement in SETUP:
                cur.execute(statement)
            print("tables created")
            return

        if stage == "report":
            cur.execute(REPORT)
            for column, value in zip([c.name for c in cur.description], cur.fetchone()):
                print(f"  {column:<22} {value}")
            return

        if stage not in STAGES:
            sys.exit(f"unknown stage {stage!r}; expected one of "
                     f"{', '.join(['setup', *STAGES, 'report'])}")

        cur.execute(STAGES[stage])
        print(f"{stage}: {cur.rowcount} rows inserted")


if __name__ == "__main__":
    main()
