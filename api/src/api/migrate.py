"""Tiny forward-only migrations: api/migrations/NNN_name.sql, each applied once, in order, at startup.

db/init/*.sql only runs when the database volume is first created; everything added later goes here.
"""

from __future__ import annotations

import logging
from importlib import resources

log = logging.getLogger("api.migrate")


async def migrate(pool) -> None:
    files = sorted(f for f in resources.files("api.migrations").iterdir() if f.name.endswith(".sql"))
    async with pool.connection() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                           "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = {r["version"] for r in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
        for f in files:
            if f.name in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text())
                await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.name,))
            log.info("applied migration %s", f.name)
