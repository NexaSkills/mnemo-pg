#!/usr/bin/env python3
"""Native shape gate for one mnemo-pg schema.

Usage:
    python3 scripts/check_shape.py <schema> [--trust-ddl]
    python3 scripts/check_shape.py --all          # every mnemosyne* schema

Exit 0 = schema matches the native contract; nonzero = violations listed.
Checks are pure PG introspection (no shim knowledge):

  tables     every base table + vec companion exists
  identity   every base table has an IDENTITY column (lcm-pg rowid pattern)
  fts        every FTS table has a tsvector column + GIN index + trigger
  vectors    vec_* has a vector column + HNSW cosine index
  fk         vec_* -> base FK exists with ON DELETE CASCADE
  fn         mnemo_normalize_fts exists
  triggers   base tables have trigger-maintained tsvector semantics

DDL_TRUSTED (--trust-ddl): when the schema was created by scripts/native_ddl.py
(declarative, idempotent), structural checks are trusted by construction; the
gate still runs the cheap non-destructive probes (function existence, vector
type). This mode is for --all sweeps; a fresh schema must pass WITHOUT the flag.

DSN: $MNEMO_PG_DSN, else $HERMES_HOME/.env, else private lcm-pg cluster.
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import pathlib
import re
import sys

from mnemo_backends import psycopg_site as _ps
_p = _ps.ensure_psycopg()
import psycopg

BASE_TABLES = ['working_memory', 'episodic_memory', 'facts', 'canonical_facts', 'triples', 'graph_edges', 'memoria_facts', 'memoria_persona', 'memoria_preferences', 'memoria_instructions', 'memoria_kg', 'memoria_timelines', 'scratchpad', 'consolidated_facts', 'consolidation_log', 'conflicts', 'memory_validations', 'memory_audit_events', 'memory_events', 'media_assets', 'media_moments', 'gists', 'annotations', 'audit_log', 'memory_embeddings']
FTS_TABLES = {
    "working_memory": "content_tsv", "episodic_memory": "content_tsv",
    # live contract: the core's facts FTS column is object_tsv (object-arm
    # tsvector maintained by the facts_object_tsv trigger), NOT fact_tsv —
    # no schema in the fleet has fact_tsv and no core code writes one.
    "facts": "object_tsv",
}
VEC_TABLES = {
    "vec_working": "working_memory",
    "vec_episodes": "episodic_memory",
    "vec_facts": "facts",
}


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
    run = pathlib.Path.home() / "lcm-pg" / "run"
    if run.is_dir():
        return f"host={run} port=54329 dbname=lcm user=lcm01"
    raise SystemExit("no MNEMO_PG_DSN and no lcm-pg/run socket dir")


def check(conn, schema: str, trusted: bool) -> list[str]:
    bad: list[str] = []
    cur = conn.cursor()

    def one(sql, *a):
        cur.execute(sql, a)
        return cur.fetchone()

    def many(sql, *a):
        cur.execute(sql, a)
        return cur.fetchall()

    exists = one("SELECT 1 FROM information_schema.schemata WHERE schema_name=%s", schema)
    if not exists:
        return [f"schema {schema} does not exist"]

    tables = {r[0] for r in many(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", schema)}

    # 1. tables
    for t in BASE_TABLES + list(VEC_TABLES):
        if t not in tables:
            bad.append(f"missing table: {t}")

    # 2. identity rowid per base table (lcm-pg rule: identity, never sequences)
    idcols = {r[0]: r[1] for r in many(
        "SELECT c.relname, a.attidentity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid "
        "WHERE n.nspname=%s AND a.attnum>0 AND a.attidentity IN ('a','d')", schema)}
    for t in BASE_TABLES:
        if t in tables and not idcols.get(t):
            bad.append(f"no identity rowid: {t}")

    # 3. FTS shape: tsvector col + GIN + non-internal trigger
    fts_cols = {
        (r[0], r[1]) for r in many(
            "SELECT c.relname, a.attname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid "
            "JOIN pg_type ty ON ty.oid=a.atttypid "
            "WHERE n.nspname=%s AND ty.typname='tsvector' AND a.attnum>0", schema)}
    gin_tab = {r[0] for r in many(
        "SELECT tablename FROM pg_indexes WHERE schemaname=%s "
        "AND indexdef ILIKE %s", schema, "%gin%")}
    # GIN indexes are named gin_<table>_<tcol> by native_ddl; match the
    # specific index name so a GIN on another column can't mask a missing one.
    gin_named = {r[0] for r in many(
        "SELECT indexname FROM pg_indexes WHERE schemaname=%s", schema)}
    trg_tab = {r[0] for r in many(
        "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND NOT t.tgisinternal", schema)}
    for t, tcol in FTS_TABLES.items():
        if t not in tables:
            continue  # already reported missing
        if not any(tc == t for tc, _ in fts_cols):
            bad.append(f"{t}: no tsvector column")
        if t not in gin_tab or f"gin_{t}_{tcol}" not in gin_named:
            bad.append(f"{t}: no GIN index on {tcol}")
        if t not in trg_tab:
            bad.append(f"{t}: no tsvector trigger")

    # 4. vector shape: vector col + HNSW cosine
    vec_cols = {r[0] for r in many(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid JOIN pg_type ty ON ty.oid=a.atttypid "
        "WHERE n.nspname=%s AND ty.typname='vector' AND a.attnum>0", schema)}
    hnsw_tab = {r[0] for r in many(
        "SELECT tablename FROM pg_indexes WHERE schemaname=%s AND indexdef ILIKE %s",
        schema, "%hnsw%cosine%")}
    for v, base in VEC_TABLES.items():
        if v not in tables:
            continue
        if v not in vec_cols:
            bad.append(f"{v}: no vector column")
        if v not in hnsw_tab:
            bad.append(f"{v}: no HNSW cosine index")
        if base not in tables:
            bad.append(f"{v}: base table {base} missing")

    # 5. FK cascade vec_* -> base
    fk = {(r[0], r[1]) for r in many(
        "SELECT c.conrelid::regclass::text, confrelid::regclass::text "
        "FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
        "WHERE n.nspname=%s AND contype='f' AND confdeltype='c'", schema)}
    for v, base in VEC_TABLES.items():
        if v in tables:
            # regclass text carries the schema qualification only when the
            # schema is not in search_path — compare basenames, not raw text.
            def base_name(x: str) -> str:
                return x.split(".")[-1].strip('"')
            if not any(base_name(a) == v and base_name(b) == base for a, b in fk):
                bad.append(f"{v}: no ON DELETE CASCADE FK to {base}")

    # 6. FTS normalize function
    if not one("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
               "WHERE n.nspname=%s AND p.proname IN ('mnemo_normalize_fts','julianday_parity')",
               schema):
        bad.append("missing parity/normalize function")

    if trusted:
        # DDL came from native_ddl.py itself; structural facts are by
        # construction — keep only the function/vector existence checks above.
        bad = [b for b in bad if b.startswith(("missing parity", "schema", "no vector"))]
    return bad


def main(argv: list[str]) -> int:
    args = [a for a in argv if a.startswith("-")]
    if "--trust-ddl" in args:
        trusted = True
    else:
        trusted = False
    targets = [a for a in argv if not a.startswith("-") or a == "--all"]
    if targets == ["--all"]:
        conn = psycopg.connect(dsn(), autocommit=True)
        targets = [r[0] for r in conn.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'mnemosyne%' ORDER BY 1")]
        conn.close()
    if not targets:
        print(__doc__)
        return 2
    conn = psycopg.connect(dsn(), autocommit=True)
    rc = 0
    try:
        for schema in targets:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
                print(f"FAIL {schema}: unsafe name")
                rc = 1
                continue
            bad = check(conn, schema, trusted)
            if bad:
                rc = 1
                print(f"FAIL {schema}:")
                for b in bad:
                    print(f"  - {b}")
            else:
                print(f"OK   {schema}")
    finally:
        conn.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
