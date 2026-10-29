#!/usr/bin/env python3
"""Migrate a SQLite mnemosyne.db into a mnemo-pg schema (increment 2).

Copies every data table 1:1 (column intersection; the PG schema is the
core's own shape plus its lazy migrations, already provisioned by
provision_schema.py + apply_add_columns.py). Then rebuilds the derived
structures natively:

  * tsvector / docsize triggers backfill (via fts_pg.ensure_fts re-run)
  * vec_working / vec_episodes / vec_facts from memory_embeddings
    (the core's own float32 JSON blobs -> pgvector, unit-normalised the
    same way the core normalises for sqlite-vec)

Rowids: SQLite implicit rowids are NOT reproduced (nothing in the core
orders by them across restarts; the rowid shim columns are identity
derivations). Episodic_memory has a real rowid column and it IS copied.

Usage:
    python3 scripts/migrate_sqlite.py <sqlite.db> <schema> [--dry]
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# tables to skip: FTS/vec virtual + shadow tables and sqlite internals
_SKIP = lambda n: (n.startswith(("fts_", "vec_", "sqlite_"))
                   or n.endswith("_fts_docsize") or n == "sqlite_master")


def dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    run = pathlib.Path.home() / "lcm-pg" / "run"
    return f"host={run} port=54329 dbname=lcm user=lcm01"


def copy_tables(sqc, cur, schema: str, dry: bool):
    cur.execute("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type='BASE TABLE'")
    pg_tables = {r[0] if not isinstance(r, dict) else next(iter(r.values()))
                 for r in cur.fetchall()}
    tables = [r[0] for r in sqc.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not _SKIP(r[0])]
    total = 0
    report = {}
    for t in sorted(tables):
        if t not in pg_tables:
            report[t] = "NO PG TABLE (skipped)"
            continue
        try:
            sq_cols = [r[1] for r in sqc.execute(f'PRAGMA table_info("{t}")')]
        except Exception as e:
            report[t] = f"pragma fail: {e}"
            continue
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (t,))
        pg_cols = {(r[0] if not isinstance(r, dict) else next(iter(r.values())))
                   for r in cur.fetchall()}
        cols = [c for c in sq_cols if c in pg_cols]
        if not cols:
            report[t] = "no common columns"
            continue
        n_src = sqc.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        if dry:
            report[t] = f"would copy {n_src} rows x {len(cols)} cols"
            continue
        collist = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        rows = sqc.execute(f'SELECT {collist} FROM "{t}"').fetchall()
        if rows:
            # GENERATED ALWAYS identity columns refuse literals unless the
            # INSERT says OVERRIDING SYSTEM VALUE.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "AND column_default LIKE 'nextval%%' LIMIT 1", (t,))
            identity = cur.fetchone() is not None
            # identity columns in PG here are GENERATED ALWAYS (schema_ddl
            # emits GENERATED ALWAYS AS IDENTITY); detect via is_identity.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "AND is_identity = 'YES' LIMIT 1", (t,))
            identity = identity or cur.fetchone() is not None
            if identity:
                _copy_overriding(cur, t, cols, rows)
            else:
                cur.executemany(
                    f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders}) '
                    f'ON CONFLICT DO NOTHING',
                    rows)
        cur.execute(f'SELECT count(*) FROM "{t}"')
        r = cur.fetchone()
        n_dst = r[0] if not isinstance(r, dict) else next(iter(r.values()))
        report[t] = f"{n_src} -> {n_dst}" + ("" if n_dst >= n_src else "  <-- MISMATCH")
        total += n_src
    return report, total


def rebuild_vectors(sqc, cur, dim: int, conn=None):
    """memory_embeddings -> vec_working/vec_episodes/vec_facts via pgvector."""
    import json
    # rowid resolution needs SQLite-style rowids; PG has none implicitly.
    # Create the shim sequences/columns directly (idempotent DDL).
    shimmed = set()
    for tbl in ("working_memory", "facts", "gists", "annotations",
                "consolidated_facts", "graph_edges", "canonical_facts"):
        try:
            cur.execute(f'CREATE SEQUENCE IF NOT EXISTS "{tbl}__rowid_seq"')
            cur.execute(
                f'ALTER TABLE "{tbl}" ADD COLUMN IF NOT EXISTS '
                f'"{tbl}__rowid" BIGINT NOT NULL DEFAULT '
                f'nextval(\'{tbl}__rowid_seq\')')
            cur.execute(
                f'SELECT 1 FROM information_schema.columns WHERE '
                f'table_schema = current_schema() AND table_name = %s '
                f'AND column_name = %s', (tbl, f"{tbl}__rowid"))
            if cur.fetchone() is not None:
                shimmed.add(tbl)
        except Exception:
            pass
    # which vec table each embedding id belongs to
    has = {}
    for src, dst in (("working", "working_memory"), ("episodes", "episodic_memory"),
                     ("facts", "facts")):
        cur.execute(f'SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                    f"WHERE n.nspname=current_schema() AND c.relname='vec_{src}'")
        has[src] = cur.fetchone() is not None
    n = sqc.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0]
    if not n:
        return "no embeddings"
    done = 0
    for (eid, emb_json) in sqc.execute(
            "SELECT memory_id, embedding_json FROM memory_embeddings"):
        try:
            vec = json.loads(emb_json)
        except Exception:
            continue
        if not isinstance(vec, list) or len(vec) != dim:
            continue
        norm = sum(x * x for x in vec) ** 0.5
        if norm == 0:
            continue
        unit = [x / norm for x in vec]
        # route by which base table owns the id (order = tier preference)
        for table, vec_table, key in (
                ("working_memory", "vec_working", "id"),
                ("episodic_memory", "vec_episodes", "id"),
                ("facts", "vec_facts", "fact_id")):
            if not has.get(vec_table.split("_", 1)[1]):
                continue
            try:
                # Raw PG has no implicit rowid. working_memory/facts use the
                # shim column created above; episodic_memory has a REAL
                # rowid identity column.
                if table == "episodic_memory":
                    r = cur.execute(
                        f'SELECT rowid FROM "{table}" WHERE "{key}" = %s',
                        (eid,)).fetchone()
                elif table in shimmed:
                    r = cur.execute(
                        f'SELECT "{table}__rowid" FROM "{table}" '
                        f'WHERE "{key}" = %s', (eid,)).fetchone()
                else:
                    continue
            except Exception:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                continue
            if r is None:
                continue
            rid = r[0] if not isinstance(r, dict) else next(iter(r.values()))
            if table == "episodic_memory":
                pass  # episodic rowid IS the real column
            cur.execute(
                f'INSERT INTO "{vec_table}" (rowid, embedding) VALUES (%s, %s::vector) '
                f'ON CONFLICT (rowid) DO UPDATE SET embedding = EXCLUDED.embedding',
                (rid, "[" + ",".join(f"{x:.7f}" for x in unit) + "]"))
            done += 1
            break
    return f"{done}/{n} embeddings placed"



def _copy_overriding(cur, t, cols, rows):
    """INSERT ... OVERRIDING SYSTEM VALUE SELECT ... ON CONFLICT DO NOTHING.

    PG grammar only accepts OVERRIDING before the data source, and the
    VALUES-list form rejects it (verified against PG 18), so rows stream
    through as `SELECT * FROM (VALUES (...),(...)) AS v(cols)` with the
    clause placed ahead of the SELECT.
    """
    collist = ", ".join(f'"{c}"' for c in cols)
    names = ", ".join(f'"{c}"' for c in cols)
    # VALUES rows typed as text via %s::text; the outer SELECT casts each
    # column to its target PG type (NULLs pass through untouched).
    cur.execute(
        "SELECT column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s", (t,))
    udt = {}
    for r in cur.fetchall():
        k, v = (r[0], r[1]) if not isinstance(r, dict) else tuple(r.items())[0][:2]
        udt[k] = v
    pg_cast = {
        "timestamp": "timestamp", "timestamptz": "timestamptz",
        "float8": "double precision", "float4": "real",
        "int8": "bigint", "int4": "integer", "bool": "boolean",
        "bytea": "bytea", "jsonb": "jsonb", "numeric": "numeric",
    }
    sel = ", ".join(
        f'("_v"."{c}"::text::{pg_cast.get(udt.get(c, "text"), "text")})'
        if (udt.get(c) or "text") not in ("text", "varchar")
        else f'"_v"."{c}"'
        for c in cols)
    one = "(" + ", ".join(["%s::text"] * len(cols)) + ")"
    B = 200
    for i in range(0, len(rows), B):
        batch = rows[i:i + B]
        vals = ", ".join(one for _ in batch)
        flat = []
        for r in batch:
            flat.extend(r.values() if isinstance(r, dict) else tuple(r))
        cur.execute(
            f'INSERT INTO "{t}" ({collist}) OVERRIDING SYSTEM VALUE '
            f'SELECT {sel} FROM (VALUES {vals}) AS _v({names}) '
            f'ON CONFLICT DO NOTHING',
            flat)


def main():
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv
    if len(args) != 2:
        raise SystemExit(__doc__)
    db_path, schema = args
    if not os.path.isfile(db_path):
        raise SystemExit(f"no such sqlite db: {db_path}")
    import psycopg
    from mnemo_backends import fts_pg

    sqc = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn = psycopg.connect(dsn(), autocommit=False)
    cur = conn.cursor()
    cur.execute(f'SET search_path = "{schema}", public')  # public: vector type lives there
    print(f"migrating {db_path} -> schema {schema}{' (dry)' if dry else ''}")
    report, total = copy_tables(sqc, cur, schema, dry)
    if not dry:
        conn.commit()
        # derived: FTS tsvector/docsize backfill (idempotent, own txns)
        conn.autocommit = True
        for table, col in (("working_memory", "content"),
                           ("episodic_memory", "content"),
                           ("facts", "object")):
            try:
                fts_pg.ensure_fts(conn, table, col)
            except Exception as e:
                print(f"  fts {table}: {str(e).splitlines()[0][:100]}")
        try:
            dim = int(os.environ.get("MNEMOSYNE_EMBEDDING_DIM", "384"))
            print("vectors:", rebuild_vectors(sqc, cur, dim, conn))
        except Exception as e:
            print(f"  vectors failed: {e}")
    for t, r in report.items():
        print(f"  {t:28s} {r}")
    print(f"total source rows: {total}")
    conn.close()
    sqc.close()


if __name__ == "__main__":
    main()
