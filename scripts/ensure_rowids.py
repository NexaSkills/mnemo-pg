#!/usr/bin/env python3
"""Assert the contract's `<table>__rowid` identity column on existing tables.

native_ddl.render only emits `CREATE TABLE IF NOT EXISTS`, so column-level
contract shapes (the bigint identity rowid) never land on a pre-existing
table. Provisioning/shape_pass call this BEFORE any index/FK statement that
references `<table>__rowid`; on a fresh or already-conformant schema every
statement is a no-op.

For each contract table T with a bigint identity rowid:
  1. CREATE TABLE IF NOT EXISTS T (<T>__rowid bigint GENERATED .. IDENTITY,
     <T>__id bigint)  — both columns carry no default, so a legacy table with
     only `id bigint` still gets the <T>__rowid twin added.
  2. ADD COLUMN IF NOT EXISTS <T>__rowid bigint
  3. fill NULLs from id (or max(id)+n), SET NOT NULL
  4. convert to GENERATED ALWAYS AS IDENTITY (START WITH max+1), skipping
     columns already carrying identity.
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ensure_rowid_identity(cur, schema: str) -> int:
    import native_ddl
    n = 0
    for table, cols, _rows in native_ddl.TABLES:
        colnames = [c.split()[0] for c in cols]
        if "id" not in colnames:
            continue
        rid = f"{table}__rowid"
        t = f"{_q(schema)}.{_q(table)}"
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {t} ({_q(rid)} bigint "
            "GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            f"{_q('id')} bigint)")
        cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {_q(rid)} bigint")
        # is it already identity-backed?
        ident = cur.execute(
            "SELECT a.attidentity FROM pg_attribute a "
            "JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s AND a.attname=%s AND a.attnum>0",
            (schema, table, rid)).fetchone()
        if ident is None or ident[0] in ("a", "d"):
            continue
        # an id-fed bigint twin (legacy shim shape) counts as an identity
        # column for the shape gate (lcm-pg pattern); no conversion needed.
        fed = cur.execute(
            "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
            "JOIN pg_class c ON c.oid=d.adrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=d.adnum "
            "WHERE n.nspname=%s AND c.relname=%s AND a.attname=%s",
            (schema, table, rid)).fetchone()
        if fed and fed[0] and fed[0].strip('"') == table and ident[0] == "":
            # default is nextval of the table's own legacy sequence naming;
            # accept identity-style feeding only via GENERATED columns, so
            # plain twin columns get the fill+identity path below unless a
            # sequence default mirrors the id column exactly.
            pass
        # fill NULLs
        cur.execute(
            f"WITH s AS (SELECT COALESCE(max(id),0) m FROM {t}) "
            f"UPDATE {t} SET {_q(rid)} = s.m + "
            f"row_number() OVER (ORDER BY id) "
            f"FROM s WHERE {_q(rid)} IS NULL AND id IS NOT NULL")
        cur.execute(f"ALTER TABLE {t} ALTER COLUMN {_q(rid)} SET NOT NULL")
        mx = cur.execute(f"SELECT COALESCE(max({_q(rid)}),0) FROM {t}").fetchone()[0]
        cur.execute(
            f"ALTER TABLE {t} ALTER COLUMN {_q(rid)} "
            f"ADD GENERATED ALWAYS AS IDENTITY (START WITH {int(mx) + 1})")
        n += 1
    return n
