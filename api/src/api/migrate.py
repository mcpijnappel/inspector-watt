"""Tiny forward-only migrations: api/migrations/NNN_name.sql, each applied once, in order, at startup.

000_schema.sql is the base schema. Databases from before it was a migration (it ran as the database image's init
script) already have its tables: there it's marked as applied without running.
"""

from __future__ import annotations

import logging
from importlib import resources

log = logging.getLogger("api.migrate")

BASE = "000_schema.sql"


async def migrate(pool) -> None:
    files = sorted(f for f in resources.files("api.migrations").iterdir() if f.name.endswith(".sql"))
    async with pool.connection() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                           "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = {r["version"] for r in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
        if BASE not in done and (await (await conn.execute("SELECT to_regclass('public.sessions') AS t")).fetchone())["t"]:
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (BASE,))
            done.add(BASE)
            log.info("base schema already present: %s marked as applied", BASE)
        for f in files:
            if f.name in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text())
                await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.name,))
            log.info("applied migration %s", f.name)
