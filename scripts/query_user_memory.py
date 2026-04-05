"""
Query long-term user memory stored in ao_user_memory.

Table: ao_user_memory
---------------------
id           – UUID primary key
app_id       – which application wrote the memory (e.g. 'rag_search')
user_id      – OIDC sub claim (keyed per Entra ID identity; 'system' when
               EasyAuth is disabled or the request had no token)
agent_name   – agent that wrote the entry
memory_type  – classification (e.g. 'observation', 'preference', 'fact')
content      – free-text memory content
created_at   – write timestamp (UTC)

Memory is append-only — rows are never updated or deleted.

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

        # ── 1. Existence check ────────────────────────────────────
        cur = await conn.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM information_schema.tables "
            "  WHERE table_schema='public' AND table_name='ao_user_memory'"
            ")"
        )
        (exists,) = await cur.fetchone()
        if not exists:
            print(
                "ao_user_memory table not found.\n"
                "The table is created on first app startup with a valid DB connection.\n"
                "Bring up the ACA apps and re-run this script."
            )
            return

        # ── 2. Per-user / per-app summary ─────────────────────────
        print("\n=== LONG-TERM MEMORY — per-user summary ===")
        cur = await conn.execute(
            "SELECT user_id, app_id, COUNT(*) AS entries, "
            "MIN(created_at) AS first_write, MAX(created_at) AS last_write "
            "FROM ao_user_memory "
            "GROUP BY user_id, app_id "
            "ORDER BY last_write DESC"
        )
        summary = await cur.fetchall()
        if summary:
            for r in summary:
                print(
                    f"  user={r[0]:42s}  app={r[1]:20s}  "
                    f"entries={r[2]:4d}  first={r[3]}  last={r[4]}"
                )
        else:
            print("  (no rows yet)")

        # ── 3. Latest 20 individual entries ───────────────────────
        print("\n=== LONG-TERM MEMORY — latest 20 entries ===")
        cur = await conn.execute(
            "SELECT created_at, app_id, user_id, agent_name, memory_type, "
            "LEFT(content, 160) AS preview "
            "FROM ao_user_memory "
            "ORDER BY created_at DESC LIMIT 20"
        )
        for r in await cur.fetchall():
            print(
                f"  [{r[0]}] app={r[1]}  user={r[2]}  "
                f"agent={r[3]}  type={r[4]}\n"
                f"    {r[5]}"
            )

        # ── 4. Per-agent memory-type breakdown ────────────────────
        print("\n=== LONG-TERM MEMORY — type breakdown per agent ===")
        cur = await conn.execute(
            "SELECT agent_name, memory_type, COUNT(*) AS cnt "
            "FROM ao_user_memory "
            "GROUP BY agent_name, memory_type "
            "ORDER BY agent_name, cnt DESC"
        )
        for r in await cur.fetchall():
            print(f"  agent={r[0]:30s}  type={r[1]:20s}  count={r[2]}")


asyncio.run(
    main(),
    loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
)
