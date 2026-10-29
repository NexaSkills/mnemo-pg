"""backfill_vec_missing — embed base rows that have no vec_* twin.

Writes through the REAL plugin loader (model pins + embeddings helper come
from the live memory provider), then rebuilds the vec_* twin natively.
This is the steady-state gap filler: consolidation deletes base rows
(cleanup_vec_orphans handles the reverse direction) and this covers rows
written while embedding generation was unavailable.

Usage: backfill_vec_missing.py <schema> [--limit N]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import psycopg  # noqa: E402

TABLES = (
    # base table, text expr to embed, vec table, base rowid col
    ("working_memory", "content", "vec_working", "working_memory__rowid"),
    ("episodic_memory", "content", "vec_episodes", "episodic_memory__rowid"),
    ("facts", "subject || ' ' || predicate || ' ' || object", "vec_facts", "facts__rowid"),
)


async def main():
    schema = sys.argv[1]
    if not schema.replace("_", "isalnum").isalnum():
        raise SystemExit("unsafe schema name")
    limit = 200
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    # Route all engine connections to PG (loads model pins from .env).
    from plugin import __file__ as _loader  # noqa: F401,E402
    import mnemo_backends.factory as factory  # noqa: E402
    from mnemosyne.core.embeddings import embed_query  # noqa: E402

    assert factory.backend_for() == "postgres", "PG backend required"

    import os
    conn = await psycopg.AsyncConnection.connect(
        host=os.path.expanduser(os.environ.get("MNEMO_PG_HOST", "~/lcm-pg/run")),
        port=int(os.environ.get("MNEMO_PG_PORT", "54329")),
        dbname="lcm", user="lcm01", autocommit=True)
    async with conn:
        async with conn.cursor() as cur:
            await cur.execute('SET search_path = "%s", public' % schema)
            for base, text_expr, vec_t, rcol in TABLES:
                await cur.execute(
                    f"SELECT b.{rcol}, {text_expr} FROM {base} b "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {vec_t} v "
                    f"WHERE v.rowid = b.{rcol}) LIMIT {int(limit)}")
                missing = await cur.fetchall()
                embedded = errors = 0
                for base_rowid, text in missing:
                    try:
                        vec = embed_query(str(text or ""))
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        print(f"  embed error: {type(exc).__name__}: {exc}")
                        continue
                    vec = [float(x) for x in __import__("numpy").asarray(vec, dtype="float32").ravel().tolist()]
                    await cur.execute(
                        f"INSERT INTO {vec_t} (rowid, embedding) VALUES (%s, %s::vector) "
                        f"ON CONFLICT (rowid) DO NOTHING", (base_rowid, vec))
                    embedded += 1
                print(f"{schema}: {base} embedded {embedded}/{len(missing)} "
                      f"(errors {errors}); vec now: ", end="")
                await cur.execute(f"SELECT count(*) FROM {vec_t}")
                print((await cur.fetchone())[0])
    await conn.close()



asyncio.run(main())
