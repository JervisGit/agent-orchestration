"""
Query agent-to-agent (A2A) runtime state tables.

Tables of interest
------------------
checkpoints         – LangGraph checkpoint records per thread/run
checkpoint_blobs    – serialised state blobs for each checkpoint
checkpoint_writes   – pending writes (channels) between checkpoints
ao_workflow_runs    – platform-level run records (ao-core)
ao_hitl_requests    – human-in-the-loop approval records

NOTE: The three LangGraph checkpoint tables are created the first time
`setup_pg_checkpointer()` runs inside a container (i.e. when an ACA
revision starts AND receives its first request).  If the tables are
absent, bring up the ACA apps and send at least one chat message.

Requirements
------------
    pip install psycopg[binary]

On Windows or macOS event-loop compatibility is handled automatically
via the SelectorEventLoop factory at the bottom of this file.
"""
import asyncio
import selectors

import psycopg

DB = (
    "host=psql-ao-dev.postgres.database.azure.com "
    "port=5432 dbname=ao user=aoadmin password=Test12345678 sslmode=require"
)


async def main() -> None:
    async with await psycopg.AsyncConnection.connect(DB) as conn:

        # ── 1. List all tables ────────────────────────────────────
        print("\n=== TABLES IN PUBLIC SCHEMA ===")
        rows = await conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        )
        tables = [r[0] for r in await rows.fetchall()]
        for t in tables:
            print(f"  {t}")

        # ── 2. LangGraph checkpoint tables (A2A state) ────────────
        lg_tables = [t for t in tables if "checkpoint" in t]
        if lg_tables:
            for t in lg_tables:
                print(f"\n=== {t.upper()} (latest 10 rows) ===")
                try:
                    cur = await conn.execute(
                        f"SELECT thread_id, checkpoint_ns, checkpoint_id, "
                        f"created_at FROM {t} "
                        f"ORDER BY created_at DESC LIMIT 10"
                    )
                    for r in await cur.fetchall():
                        print(
                            f"  thread={r[0][:12]}...  ns={r[1]!r}  "
                            f"ckpt={r[2][:12]}...  at={r[3]}"
                        )
                except Exception as e:
                    print(f"  (columns differ or empty) {e}")
        else:
            print(
                "\n  LangGraph checkpoint tables not yet created.\n"
                "  Send a chat message to the graph-compliance or rag-search app\n"
                "  and re-run this script."
            )

        # ── 3. ao_workflow_runs ───────────────────────────────────
        if "ao_workflow_runs" in tables:
            print("\n=== AO_WORKFLOW_RUNS (latest 20 rows) ===")
            cur = await conn.execute(
                "SELECT run_id, app_id, workflow_id, status, "
                "started_at, completed_at "
                "FROM ao_workflow_runs "
                "ORDER BY started_at DESC LIMIT 20"
            )
            rows_data = await cur.fetchall()
            if rows_data:
                for r in rows_data:
                    duration = (
                        f"  dur={(r[5]-r[4]).total_seconds():.1f}s"
                        if r[4] and r[5]
                        else ""
                    )
                    print(
                        f"  [{r[3]:10s}] run={r[0][:8]}  app={r[1]}  "
                        f"wf={r[2]}{duration}"
                    )
            else:
                print("  (no rows yet)")

        # ── 4. ao_hitl_requests ───────────────────────────────────
        if "ao_hitl_requests" in tables:
            print("\n=== AO_HITL_REQUESTS (all rows) ===")
            cur = await conn.execute(
                "SELECT request_id, workflow_id, step_name, status, "
                "requested_at, resolved_at "
                "FROM ao_hitl_requests "
                "ORDER BY requested_at DESC"
            )
            for r in await cur.fetchall():
                resolved = f"  resolved={r[5]}" if r[5] else ""
                print(
                    f"  [{r[3]:10s}] req={r[0][:8]}  wf={r[1]}  "
                    f"step={r[2]}  at={r[4]}{resolved}"
                )


asyncio.run(
    main(),
    loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
)
