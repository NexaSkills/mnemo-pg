"""Provision a mnemo-pg schema (idempotent) and verify it.

Usage:
    python3 scripts/provision_schema.py <schema>            # provision + verify
    python3 scripts/provision_schema.py --all               # every mnemosyne* schema present

Layers (in order):
  1. CREATE SCHEMA + catalog shims   (postgres._provision_probe — same code
                                      the live connect path runs)
  2. core tables                     (schema_ddl.DDL   — 24 tables)
  3. indexes                         (schema_ddl.INDEXES)
  4. pgvector companions             (vec_working / vec_episodes / vec_facts:
                                      rowid BIGINT PK + vector(DIM) + HNSW)
  5. FTS helpers + per-table tsvector triggers (fts_pg.ensure_fts over the
                                      three core FTS shapes)
"""
import os
import re
import sys
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# the three FTS5 shapes the vendored core declares (beam.py):
#  - fts_working  standalone over working_memory.content
#  - fts_episodes external-content over episodic_memory.content by rowid
#  - fts_facts    external-content over facts.body
FTS_TARGETS = (("working_memory", "content"),
               ("episodic_memory", "content"),
               ("facts", "object"))

VEC_TABLES = ("working", "episodes", "facts")  # vec_working, vec_episodes, vec_facts


def dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    p = pathlib.Path(home) / ".env"
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.startswith("MNEMO_PG_DSN="):
                return line.split("=", 1)[1].strip()
    # fleet default: the private lcm-pg cluster
    run = pathlib.Path.home() / "lcm-pg" / "run"
    if run.is_dir():
        return f"host={run} port=54329 dbname=lcm user=lcm01"
    raise SystemExit("no MNEMO_PG_DSN")


def translate(sql: str) -> str:
    """SQLite DDL text -> PG DDL text via the production translator."""
    from mnemo_backends.postgres import _rewrite_text
    return _rewrite_text(sql)


def core_tables_present(cur):
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind='r'")
    return {r[0] for r in cur.fetchall()}


def vec_ddl(dim: int) -> list:
    out = []
    for t in VEC_TABLES:
        out.append(
            f'CREATE TABLE IF NOT EXISTS "vec_{t}" ('
            f'rowid BIGINT PRIMARY KEY, embedding vector({int(dim)}))')
        out.append(
            f'CREATE INDEX IF NOT EXISTS "hnsw_vec_{t}" ON "vec_{t}" '
            f"USING hnsw (embedding vector_cosine_ops)")
    return out


def table_names():
    return [re.search(r'CREATE TABLE(?: IF NOT EXISTS)? "?([a-zA-Z_0-9]+)', d).group(1)
            for d in __import__("mnemo_backends.schema_ddl", fromlist=["DDL"]).DDL]


def provision(schema: str, dim: int = 384, verbose: bool = True) -> dict:
    import psycopg
    from mnemo_backends import schema_ddl, postgres
    from mnemo_backends import fts_pg

    if not re.fullmatch(r"[a-z][a-z0-9_]*", schema):
        raise SystemExit(f"unsafe schema name: {schema}")
    conn = psycopg.connect(dsn(), autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(f'SET search_path = "{schema}", public')
        postgres._ensure_catalog_shims(cur, schema)

        have = core_tables_present(cur)
        made = 0
        for ddl in schema_ddl.DDL:
            m = re.search(r'CREATE TABLE(?: IF NOT EXISTS)? "?([a-zA-Z_0-9]+)', ddl)
            if not m:
                raise RuntimeError(f"unparseable DDL entry: {ddl[:80]}")
            if m.group(1) in have:
                continue
            cur.execute(translate(ddl))
            made += 1

        idx = 0
        for i in schema_ddl.INDEXES:
            try:
                cur.execute(translate(i))
                idx += 1
            except Exception as e:
                print(f"  index skip: {str(e).splitlines()[0][:120]}")

        for v in vec_ddl(dim):
            cur.execute(v)

        fts_pg.ensure_helper(conn)
        fts = 0
        for table, col in FTS_TARGETS:
            try:
                fts_pg.ensure_fts(conn, table, col)
                fts += 1
            except Exception as e:
                print(f"  fts skip {table}.{col}: {str(e).splitlines()[0][:120]}")

        have = core_tables_present(cur)
        missing = [t for t in table_names() if t not in have]
        if verbose:
            print(f"schema {schema}: +{made} new tables ({len(schema_ddl.DDL)} total, "
                  f"{idx} index stmts, {fts}/{len(FTS_TARGETS)} fts, "
                  f"{len(VEC_TABLES)} vec companions)")
        if missing:
            raise SystemExit(f"MISSING TABLES: {missing}")
        return {"tables": len(schema_ddl.DDL), "made": made, "indexes": idx, "fts": fts}
    finally:
        conn.close()


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    dim = int(os.environ.get("MNEMOSYNE_EMBEDDING_DIM", "384"))
    if args == ["--all"]:
        import psycopg
        conn = psycopg.connect(dsn(), autocommit=True)
        rows = conn.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'mnemosyne%' ORDER BY 1").fetchall()
        conn.close()
        schemas = [r[0] for r in rows]
    else:
        schemas = args
    for s in schemas:
        provision(s, dim=dim)


if __name__ == "__main__":
    main()
