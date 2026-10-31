#!/usr/bin/env python3
"""Deploy-time fix for the 'pinned' class of mismatch (NOT NULL, no default).

Root cause (2026-10-31 kim): the vendored core's runtime guard
beam._add_column_if_missing compares an existing column's NOT-NULLness
against the declaration's *leading* token — it scans the pieces for a
standalone 'NOT NULL' and defaults expected_notnull=False otherwise.
Declarations written '<TYPE> DEFAULT 0' (no NOT NULL) therefore demand a
NULLABLE column; a column that is NOT NULL with no default FAILS the
guard on every connect ('expected INTEGER DEFAULT 0, got INTEGER
DEFAULT None') and provider init dies.

Local schemas are nullable+DEFAULT 0 (pass). kim's goldie/demo are
NOT NULL+no default (fail). The fix restores the pass-compatible shape:
backfill NULLs (0 for int, '' for text, false for bool), DROP the NOT
NULL constraint, and set the core's own DEFAULT from the declaration.
Idempotent and data-safe (no drops, no type rewrites).
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mnemo_backends.core_declared import declared_columns  # noqa: E402

import psycopg
from psycopg import sql

# The literals below (sql.SQL(lit/backfill)) derive from the core's OWN
# declarations parsed by core_declared (type/default tokens, never user
# input), and every identifier is wrapped in sql.Identifier.
DSN = "postgresql://lcm01@/lcm?host=/home/mybuddy/lcm-pg/run&port=54329"
RE = re.compile(r"(?i)\bDEFAULT\s+(?::?'([^']*)'|([A-Za-z0-9_.+-]+))")


def _default_lit(decl):
    m = RE.search(decl or "")
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def main():
    import psycopg
    decls = declared_columns()
    fixes = 0
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "select nspname from pg_namespace "
            "where nspname like 'mnemosyne\\_%' escape '\\' "
            "and nspname not like '%\\_test%' escape '\\'")
        schemas = [r[0] for r in cur.fetchall()]
        for schema in schemas:
            for table, cols in sorted(decls.items()):
                if "_" in table:
                    continue  # core tables are bare names; skip FTS/aux shadows
                for column, decl in sorted(cols.items()):
                    if "DEFAULT" not in decl.upper():
                        continue
                    want = _default_lit(decl)
                    if want is None:
                        continue
                    cur.execute(
                        "select a.attnotnull, "
                        "true, "
                        "coalesce(pg_get_expr(ad.adbin, ad.adrelid), 'NONE') "
                        "from pg_class t "
                        "join pg_namespace n on n.oid = t.relnamespace "
                        "  and n.nspname = %s "
                        "join pg_attribute a on a.attrelid = t.oid "
                        "  and a.attname = %s and a.attnum > 0 "
                        "  and not a.attisdropped "
                        "left join pg_attrdef ad on ad.adrelid = t.oid "
                        "  and ad.adnum = a.attnum "
                        "where t.relname = %s",
                        (schema, column, table))
                    row = cur.fetchone()
                    if not row:
                        continue
                    notnull, _istype, dflt = row
                    # The core's runtime SQL is SQLite-shaped: it compares
                    # flag columns literally (pinned = 0). A PG boolean
                    # column has no `boolean = integer` operator, so every
                    # boolean column must be bigint (false->0, true->1).
                    cur.execute(
                        "select format_type(a.atttypid, a.atttypmod) "
                        "from pg_class t "
                        "join pg_namespace n on n.oid = t.relnamespace "
                        "  and n.nspname = %s "
                        "join pg_attribute a on a.attrelid = t.oid "
                        "  and a.attname = %s and t.relname = %s",
                        (schema, column, table))
                    pgtype_row = cur.fetchone()
                    pgtype = pgtype_row[0] if pgtype_row else None
                    if pgtype == "boolean" and (table, column) != ("working_memory", "pinned"):
                        print(f"SKIP {schema}.{table}.{column} "
                              "boolean (out of pinned scope)")
                        continue
                    # declare a PG-side default that satisfies BOTH the core
                    # guard (nullable + declared default) and sqlite-proxy
                    # PRAGMA rendering (plain literal for int defaults).
                    lit = want
                    if dflt.upper().startswith("NULL::"):
                        dflt_ok = lit in ("NULL",)
                    else:
                        dflt_ok = dflt in (lit, f"'{lit}'::character varying",
                                          f"'{lit}'::text", f"{lit}::bigint",
                                          f"{lit}::integer", f"{lit}::boolean")
                    if not (notnull or not dflt_ok):
                        continue
                    base = lit.lower()
                    backfill = {"null": "NULL", "false": "false", "true": "true"}.get(
                        base, lit if re.fullmatch(r"[0-9.+-]+|NULL", lit) else f"'{lit}'")
                    if pgtype == "boolean":
                        # boolean -> bigint so the core's `pinned = 0` compiles
                        cur.execute(
                            sql.SQL("UPDATE {}.{} SET {} = "
                                    "CASE WHEN {} THEN 1 ELSE 0 END "
                                    "WHERE {} IS NOT NULL").format(
                                        sql.Identifier(schema),
                                        sql.Identifier(table),
                                        sql.Identifier(column),
                                        sql.Identifier(column),
                                        sql.Identifier(column)))
                        cur.execute(
                            # boolean::bigint has no cast — must map values
                            sql.SQL('ALTER TABLE {}.{} ALTER COLUMN {} '
                                    "TYPE bigint USING CASE WHEN {} THEN 1 "
                                    "ELSE 0 END").format(
                                        sql.Identifier(schema),
                                        sql.Identifier(table),
                                        sql.Identifier(column),
                                        sql.Identifier(column)))
                    cur.execute(
                        sql.SQL('ALTER TABLE {}.{} ALTER COLUMN {} SET DEFAULT {}').format(
                            sql.Identifier(schema), sql.Identifier(table),
                            sql.Identifier(column), sql.SQL(lit)))
                    cur.execute(
                        sql.SQL('UPDATE {}.{} SET {} = {} WHERE {} IS NULL').format(
                            sql.Identifier(schema), sql.Identifier(table),
                            sql.Identifier(column), sql.SQL(backfill),
                            sql.Identifier(column)))
                    cur.execute(
                        sql.SQL('ALTER TABLE {}.{} ALTER COLUMN {} DROP NOT NULL').format(
                            sql.Identifier(schema), sql.Identifier(table),
                            sql.Identifier(column)))
                    fixes += 1
                    print(f"FIX {schema}.{table}.{column} -> "
                          f"DROP NOT NULL, DEFAULT {lit}")
    print(f"nullable-default-shape fixes: {fixes}")


if __name__ == "__main__":
    main()
