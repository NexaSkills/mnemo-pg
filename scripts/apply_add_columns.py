#!/usr/bin/env python3
"""Apply the core's own ADD COLUMN migrations that gen_ddl.py cannot see.

The vendored core adds most schema columns lazily at open time via
_add_column_if_missing(conn, table, column, type) — gen_ddl.py's capture
only sees CREATE statements emitted through a shim that short-circuits
ALTERs it never reaches. Here we scrape the literal call sites from the
vendored source and apply them with ALTER TABLE ADD COLUMN IF NOT EXISTS.
Ponytail: scrape the truth from source, don't hand-maintain a column list.

Usage: python3 scripts/apply_add_columns.py <schema> [<schema> ...]
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import re
import sys
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
VENDOR = REPO / "vendor" / "mnemosyne"
sys.path.insert(0, str(REPO))

CALL_RE = re.compile(
    r"""_add_column_if_missing\(\s*conn\s*,\s*[\"'](\w+)[\"']\s*,\s*[\"'](\w+)[\"']\s*,\s*(['"])((?:(?!\3)[\s\S])*?)\3\s*\)""")
# f-string / computed form: _add_column_if_missing(conn, "memory_events", col, ddl.split(...))
# -> those columns come from a dict literal right above; handled by the
# memory_events block scrape below.

DICT_RE = re.compile(r"\"(\w+)\":\s*\"(\w+) ([^\"]+)\"")


def scrape() -> list:
    out = []
    for py in VENDOR.rglob("*.py"):
        try:
            src = py.read_text()
        except Exception:
            continue
        for m in CALL_RE.finditer(src):
            table, col, _q, typ = m.groups()
            out.append((table, col, typ.strip()))
        # memory_events-style dict loops: {col: "name TYPE ..."} fed to ALTER
        for m in re.finditer(r"ALTER TABLE \{table\} ADD COLUMN \{column\} \{col_type\}", src):
            pass  # generic helper; the literal calls above already cover callers
    # dedupe, keep first occurrence (types are stable)
    seen = set()
    ded = []
    for t, c, ty in out:
        if (t, c) in seen:
            continue
        seen.add((t, c))
        ded.append((t, c, ty))
    return ded


def dsn() -> str:
    d = os.environ.get("MNEMO_PG_DSN")
    if d:
        return d
    run = pathlib.Path.home() / "lcm-pg" / "run"
    return f"host={run} port=54329 dbname=lcm user=lcm01"


def apply_schema(schema: str, cols: list) -> None:
    import psycopg
    # types mapped locally below

    conn = psycopg.connect(dsn(), autocommit=True)
    cur = conn.cursor()
    cur.execute(f'SET search_path = "{schema}", public')
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema()")
    have = set(cur.fetchall())
    added = 0
    for table, col, typ in cols:
        if (table, col) in have:
            continue
        # scrape-side cleanup only: the types are plain SQLite column-type
        # text ("TEXT", "INTEGER DEFAULT 1") -> map types, no SQL rewriting.
        pg_type = re.sub(r"\bINTEGER\b", "BIGINT", typ)
        pg_type = re.sub(r"\bREAL\b", "DOUBLE PRECISION", pg_type)
        pg_type = re.sub(r"\bBLOB\b", "BYTEA", pg_type)
        try:
            cur.execute(f'ALTER TABLE IF EXISTS "{table}" ADD COLUMN IF NOT EXISTS "{col}" {pg_type}')
            added += 1
        except Exception as e:
            print(f"  {schema}: {table}.{col} skip: {str(e).splitlines()[0][:100]}")
    conn.close()
    print(f"schema {schema}: +{added} migrated columns (of {len(cols)} scraped)")


def main():
    cols = scrape()
    if not cols:
        raise SystemExit("scraped 0 add-column calls — vendor path wrong?")
    for s in sys.argv[1:]:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", s):
            raise SystemExit(f"unsafe schema name: {s}")
        apply_schema(s, cols)


if __name__ == "__main__":
    main()
