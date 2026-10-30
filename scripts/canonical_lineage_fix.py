#!/usr/bin/env python3
"""Codify the 2026-10-30 manny/tessa canonical_facts lineage fixes so a
re-provision (or a fresh host rollout) lands them declaratively instead of
relying on the manual psql ALTERs applied live.

Two divergences from the healthy reference lineage (mnemosyne_debbie):

  1. PRIMARY KEY landed on the <table>__rowid twin instead of the core's
     `id` column. NativeConn._identity_col() picks the first sequence-owned
     column by attnum for INSERT ... RETURNING, so lastrowid returned a
     twin sequence value that matched no `id` -> CanonicalStore.remember()'s
     SELECT-by-id readback returned None.
     Fix: when the table has an identity `id` column but the PK sits on a
     different column, move the PK to `id` (the twin keeps its identity
     sequence but is no longer the PK).

  2. NOT NULL on `updated_at` (a legacy column the vendored core's INSERT
     never supplies) -> NotNullViolation on every canonical write.
     Reference lineage has updated_at nullable.
     Fix: DROP NOT NULL (relaxation only; existing rows untouched).

All statements are catalog-guarded DO blocks: no-ops on a conformant
schema. Run after provision_schema.py. Identity-`id` tables without a PK
get one added.
"""
from __future__ import annotations
import os
import re
import sys

import psycopg
from psycopg import sql as psql_sql

_SCHEMA_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")


def fix_statements(schema: str) -> list[str]:
    if not _SCHEMA_RE.fullmatch(schema):
        raise SystemExit(f"unsafe schema name: {schema}")
    return [
        # 1. canonical_facts PK -> id when id is identity and the PK sits
        #    elsewhere (divergent lineage); or PK missing entirely.
        "DO $$"
        "BEGIN"
        " IF EXISTS (SELECT 1 FROM pg_attribute a"
        "     JOIN pg_class c ON c.oid=a.attrelid"
        "     JOIN pg_namespace n ON n.oid=c.relnamespace"
        f"    WHERE n.nspname='{schema}' AND c.relname='canonical_facts'"
        "      AND a.attname='id' AND a.attnum>0"
        "      AND a.attidentity IN ('a','b'))"
        " AND NOT EXISTS (SELECT 1 FROM pg_index i"
        "     JOIN pg_class c ON c.oid=i.indrelid"
        "     JOIN pg_namespace n ON n.oid=c.relnamespace"
        "     JOIN pg_attribute pa ON pa.attrelid=c.oid"
        "       AND pa.attnum = ANY(i.indkey)"
        f"    WHERE n.nspname='{schema}' AND c.relname='canonical_facts'"
        "      AND i.indisprimary AND pa.attname='id')"
        " THEN"
        "   IF EXISTS (SELECT 1 FROM pg_index i"
        "       JOIN pg_class c ON c.oid=i.indrelid"
        "       JOIN pg_namespace n ON n.oid=c.relnamespace"
        f"      WHERE n.nspname='{schema}' AND c.relname='canonical_facts'"
        "        AND i.indisprimary)"
        "   THEN"
        "     EXECUTE 'ALTER TABLE " + schema + ".canonical_facts"
        "       DROP CONSTRAINT ' || ("
        "         SELECT quote_ident(ic.relname) FROM pg_index i"
        "           JOIN pg_class c ON c.oid=i.indrelid"
        "           JOIN pg_namespace n ON n.oid=c.relnamespace"
        "           JOIN pg_class ic ON ic.oid=i.indexrelid"
        f"          WHERE n.nspname='{schema}'"
        "            AND c.relname='canonical_facts'"
        "            AND i.indisprimary LIMIT 1);"
        "   END IF;"
        "   EXECUTE 'ALTER TABLE " + schema + ".canonical_facts"
        "     ADD CONSTRAINT canonical_facts_pkey PRIMARY KEY (id)';"
        " END IF;"
        "END$$",
        # 2. updated_at: relax NOT NULL (core INSERT doesn't supply it).
        "DO $$"
        "BEGIN"
        " IF EXISTS (SELECT 1 FROM pg_attribute a"
        "     JOIN pg_class c ON c.oid=a.attrelid"
        "     JOIN pg_namespace n ON n.oid=c.relnamespace"
        f"    WHERE n.nspname='{schema}' AND c.relname='canonical_facts'"
        "      AND a.attname='updated_at' AND a.attnum>0 AND a.attnotnull)"
        " THEN"
        "   EXECUTE 'ALTER TABLE " + schema + ".canonical_facts"
        "     ALTER COLUMN updated_at DROP NOT NULL';"
        " END IF;"
        "END$$",
    ]


def main() -> int:
    dsn = os.environ.get(
        "MNEMO_PG_DSN",
        "host=/home/mybuddy/lcm-pg/run port=54329 user=lcm01 dbname=lcm",
    )
    schemas = sys.argv[1:] or ["mnemosyne_manny", "mnemosyne_tessa"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        for s in schemas:
            for stmt in fix_statements(s):
                conn.execute(psql_sql.SQL(stmt))
            print(f"codified: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
