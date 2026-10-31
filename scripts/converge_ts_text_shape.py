#!/usr/bin/env python3
"""Converge every core 'timestamp'-column to TEXT in every mnemosyne schema.

Why: the vendored core (beam.py) declares working_memory.timestamp as TEXT
and its recall chronology SQL calls datetime(trim(timestamp)). On a
timestamptz twin, trim() resolves to pg_catalog.btrim(timestamptz) BEFORE
schema-local datetime overloads get a chance (Python-side SQL rewriting is
parse-blind), so recall dies with 'function pg_catalog.btrim(timestamp with
time zone) does not exist'. Provisioned schemas (native_ddl.py emits
`timestamp timestamptz`) hit this; live working schemas are text and fine.

Fix (declarative, idempotent): ALTER COLUMN ... TYPE text via a
to_char round-trip so ISO text lands (what the core's own writers use).
Also fixes audit_log.timestamp double-precision twins (same trim() crash
class) to text.

Shared-schema policy (F-C, 2026-10-31): mnemosyne_shared is NO LONGER
excluded. The original exclusion (added in ee92c6a) had no documented
reason; repo-wide search shows no shared-specific dependency on the
timestamptz shape (shared is not touched by migrate_sqlite_to_pg, and
shared_surface_pg/factory treat it as a normal schema). The exclusion was
also provably ineffective anyway: converge_core_contract_shape.py sweeps
`nspname like 'mnemosyne_%'` with no exclusion and casts timestamp text->
timestamp there (the text->timestamp comparison arm), so a fresh provision
re-flipped shared to timestamptz while 5g "skipped" it. Applying the
converge here makes shared correct at step 5g; 5c may still re-flip it on
later runs (tracked as a follow-up on the 5c sweep), which is why the
shared leg must run LAST. --exclude-schema can restore the old behaviour.

Usage: converge_ts_text_shape.py [--dsn DSN] [--dry-run]
                                 [--exclude-schema NAME ...]
"""
import argparse
import re
import sys

import psycopg
import psycopg.sql

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DSN = ("postgresql://lcm01@/lcm?host=/home/mybuddy/lcm-pg/run"
               "&port=54329")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# core sqlite columns named 'timestamp' that must be TEXT everywhere
TABLES = ("working_memory", "episodic_memory", "facts", "audit_log",
          "memory_events")


def _expr(cur, schema, table):
    cur.execute(
        "select format_type(a.atttypid, a.atttypmod) from pg_attribute a "
        "join pg_class c on c.oid = a.attrelid "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = %s and c.relname = %s and a.attname = 'timestamp' "
        "and a.attnum > 0 and not a.attisdropped", (schema, table))
    return cur.fetchone()


def _to_text_expr(cur, schema, table, pgtype):
    # ISO text round-trip; timestamp families render UTC-style, others cast.
    if pgtype and pgtype.startswith(("timestamp", "date")):
        # plain cast: 'YYYY-MM-DD HH:MM:SS+00' ISO text, matching what the
        # core's own writers store in the text twin
        return "timestamp::text"
    return "timestamp::text"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--exclude-schema", action="append", default=[],
                    help="skip this schema (repeatable); restores the "
                         "pre-F-C behaviour when given mnemosyne_shared")
    args = ap.parse_args()

    fixed = 0
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select nspname from pg_namespace "
                "where nspname like 'mnemosyne!_%%' escape '!' order by 1")
            schemas = [r[0] for r in cur.fetchall()
                       if r[0] not in set(args.exclude_schema)]
            for s in schemas:
                if not _NAME.match(s):
                    continue
                for t in TABLES:
                    row = _expr(cur, s, t)
                    if not row:
                        continue
                    pgtype = row[0]
                    if pgtype == "text":
                        continue
                    expr = _to_text_expr(cur, s, t, pgtype)
                    sql = (f'alter table "{s}"."{t}" '
                           f"alter column timestamp type text "
                           f"using coalesce({expr}, '')")
                    if args.dry_run:
                        print(f"DRY {s}.{t}: {sql}")
                        continue
                    try:
                        cur.execute(psycopg.sql.SQL(sql))
                        print(f"FIX {s}.{t}.timestamp {pgtype} -> text")
                        fixed += 1
                    except Exception as exc:
                        print(f"SKIP {s}.{t}.timestamp ({pgtype}): {exc}"
                              .splitlines()[0])
    print(f"ts-text-shape fixes: {fixed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
