"""cleanup_vec_orphans — delete vec_* rows whose base row no longer exists.

vec_* tables mirror the base rowid, but the FK was added after orphans
already accumulated (consolidation deletes base rows without deleting the
vec twin; see mnemo-pg notes). This script restores 1:1 coverage in place.
"""
from __future__ import annotations

import asyncio

import sys

from psycopg import AsyncConnection

import sys as _sys, os
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemo_backends import psycopg_site as _ps  # sys.path fixup under -I

PAIRS = (
    ("vec_working", "working_memory", "working_memory__rowid"),
    ("vec_episodes", "episodic_memory", "episodic_memory__rowid"),
    ("vec_facts", "facts", "facts__rowid"),
)


async def main():
    schema = sys.argv[1]
    if not schema.replace("_", "isalnum").isalnum():
        raise SystemExit("unsafe schema name")
    conn = await AsyncConnection.connect(
        host=os.path.expanduser(os.environ.get("MNEMO_PG_HOST", "~/lcm-pg/run")),
        dbname="lcm", user="lcm01")
    async with conn:
        async with conn.cursor() as cur:
            await cur.execute('SET search_path = "%s", public' % schema)
            for vec_t, base, rcol in PAIRS:
                await cur.execute(
                    f"DELETE FROM {vec_t} v WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {base} b WHERE b.{rcol} = v.rowid)")
                print(f"{schema}: {vec_t} removed {cur.rowcount} orphan rows")
    await conn.close()


asyncio.run(main())
